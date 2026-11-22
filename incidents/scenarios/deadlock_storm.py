"""Deadlock storm: a new batch job's two workers lock rows in opposite order.

Story: a new "inventory sync" job (two parallel workers) adjusts inventory
rows. Worker A updates product X then product Y; worker B updates Y then X.
When they overlap, each holds the lock the other needs: a deadlock. SQL
Server picks one as the victim (error 1205) and rolls it back; the job
retries, and it happens again, every few seconds.

What it does: two connections as the app's login (shoplite_app, which the job
would use) with the program name "ShopLite-InventorySync/2.3", repeating
rounds: both start together, update their first row, pause, update their
second. Every round ends with ROLLBACK, and the updates set columns to their
own values: no data changes, ever. A round every 10 seconds.

Session-only: stopping the injector stops the workers; nothing is left behind.
"""

import threading

from framework import Context, Incident

PROGRAM = "ShopLite-InventorySync/2.3"
ROUND_SECONDS = 10
UPDATE = "UPDATE dbo.inventory SET reorder_level = reorder_level WHERE product_id = ?;"

PRODUCTS = """
SELECT TOP (20) product_id FROM dbo.inventory ORDER BY product_id DESC;
"""


class DeadlockStorm(Incident):
    name = "deadlock-storm"
    title = "Deadlock storm: two sync workers update inventory in opposite order"
    runs_on = ("vm",)
    default_minutes = 15
    max_minutes = 30
    root_cause = ("A new inventory sync job runs two workers (program ShopLite-InventorySync/2.3, login "
                  "shoplite_app) that update the same dbo.inventory rows in opposite orders inside "
                  "transactions, so they deadlock repeatedly; SQL Server kills one each time (error 1205).")
    correct_fix = ("Make the job touch rows in one consistent order (e.g. ORDER BY product_id), or update them "
                   "in a single statement, and keep each transaction short; until fixed, run one worker. "
                   "The deadlock graphs name both statements and the rows.")
    expected_signals = [
        "alert shoplite-sql-deadlocks",
        "SqlXEvents_CL: xml_deadlock_report every round; the graph shows two ShopLite-InventorySync/2.3 "
        "sessions, KEY locks on dbo.inventory, UPDATE statements in opposite order",
        "SqlPerfCounters_CL: Number of Deadlocks/sec above 0",
        "SqlSessions_CL: a new program ShopLite-InventorySync/2.3",
    ]

    def __init__(self):
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.deadlocks = 0
        self.rounds = 0

    def start(self, ctx: Context) -> None:
        reader = ctx.sql(PROGRAM, login="app")
        try:
            ids = [row[0] for row in reader.cursor().execute(PRODUCTS).fetchall()]
        finally:
            reader.close()
            ctx.sessions.pop()
        pairs = [(ids[i], ids[i + 1]) for i in range(0, len(ids) - 1, 2)]
        ctx.details["product_pairs"] = pairs
        ctx.do(f"start two 'InventorySync' workers (shoplite_app) updating {len(pairs)} pairs of inventory rows "
               f"in opposite orders every {ROUND_SECONDS} s, each round rolled back",
               lambda: self._launch(ctx, pairs))

    def _launch(self, ctx: Context, pairs) -> None:
        barrier = threading.Barrier(2)
        connections = [ctx.sql(PROGRAM, login="app", autocommit=False) for _ in range(2)]
        for c in connections:
            c.cursor().execute("SET LOCK_TIMEOUT 10000;")
        for worker, connection in enumerate(connections):
            thread = threading.Thread(target=self._worker, args=(worker, connection, pairs, barrier), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _worker(self, worker: int, connection, pairs, barrier) -> None:
        import time
        number = 0
        while not self.stop.is_set():
            first, second = pairs[number % len(pairs)]
            if worker == 1:
                first, second = second, first           # the bug: the other worker goes the other way
            try:
                barrier.wait(timeout=30)                # both start the round together
                cursor = connection.cursor()
                cursor.execute(UPDATE, first)
                time.sleep(0.5)                         # "processing" between the two updates
                cursor.execute(UPDATE, second)
            except threading.BrokenBarrierError:
                break
            except Exception as error:                  # 1205: this worker was the deadlock victim
                if "1205" in str(error):
                    self.deadlocks += 1
            finally:
                try:
                    connection.rollback()               # never keep anything
                except Exception:
                    pass
            if worker == 0:
                self.rounds += 1
            number += 1
            self.stop.wait(ROUND_SECONDS)
        connection.close()

    def hold(self, ctx: Context) -> None:
        reported = 0
        while not ctx.should_stop():
            ctx.wait(60)
            if self.deadlocks != reported:
                ctx.note(f"{self.rounds} rounds, {self.deadlocks} deadlocks so far")
                reported = self.deadlocks

    def revert(self, ctx: Context) -> None:
        if not self.threads:
            ctx.note("no workers running from this run")
            return

        def stop_workers():
            self.stop.set()
            for thread in self.threads:
                thread.join(timeout=30)
        ctx.do(f"stop both workers ({self.rounds} rounds, {self.deadlocks} deadlocks)", stop_workers)


INCIDENT = DeadlockStorm()
