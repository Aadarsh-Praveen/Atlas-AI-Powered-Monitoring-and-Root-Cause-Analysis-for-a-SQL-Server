# CLAUDE.md: rules for Claude Code on this project

## Project

**Atlas: AI-Powered Monitoring and Root-Cause Analysis for a SQL Server**

Atlas is a team of AI agents built on Microsoft Foundry that monitors a production SQL Server,
investigates problems on its own, explains the root cause, and recommends fixes. A human applies
every fix. The monitored system is **ShopLite**, a real e-commerce app (Next.js storefront on Vercel,
FastAPI backend and SQL Server 2022 on one Azure Windows VM) with simulated shoppers.

Read `docs/00-project-overview.md` for the full architecture and decisions.

## How we work

The project owner is building this to **learn**. The division of work is strict:

| Who | Does |
|---|---|
| Claude Code (you) | Writes code for the current phase, one file at a time, and explains it |
| Project owner | All hands-on setup: cloud resources, installs, configuration, connecting services, deploying, testing |
| Planning (outside this repo) | Architecture, decisions, and the phase files in `docs/` |

## Rules

### Scope
1. Only build what the **current phase file** in `docs/` lists under "What Claude Code builds".
   Do not build ahead into later phases, even if it seems helpful.
2. If the phase file is unclear, incomplete, or seems wrong, **stop and ask**. Do not guess.
3. Do not change architecture, tech stack, or file layout decisions from the overview or phase file.
   If you think a decision should change, explain why and ask first.

### One file at a time
4. Before writing a file, explain in a few sentences: what the file is for, where it fits in the
   system, and what it will contain.
5. Write the file with clear comments. Comment the *why*, not just the *what*.
6. After writing it, give a **block-by-block walkthrough**: each section, what it does, and why it is
   written that way. Point out anything that matters later (for example, "Atlas will use this
   session tag to identify the app version").
7. End each file with **"What you should now understand"**: 2 to 4 short points the owner should be
   able to explain in their own words.
8. Wait for the owner to say "next" (or ask questions) before starting the next file.

### Hands off the environment
9. **Never** create, change, or delete cloud resources. Never run `az`, `azd`, `vercel`, `gh`,
   `terraform`, or deployment commands. Never push to git.
10. Do not install packages, start containers, or run servers unless the owner asks. When something
    needs to be run, give the exact command and explain what it does, so the owner runs it.
11. When a step requires connecting, configuring, or deploying something, describe what the owner
    needs to do instead of doing it.

### Code standards
12. Never hardcode secrets, passwords, connection strings, or keys. Use environment variables and
    provide a `.env.example` with placeholder values. `.env` files are never committed.
13. Prefer simple, readable code over clever code. The owner should be able to follow every line.
14. Use the stack in the overview: Python 3.12 (FastAPI, SQLAlchemy, pyodbc), Next.js with JavaScript,
    T-SQL for SQL Server, PowerShell for Windows server scripts, Bicep for Azure infrastructure.
15. Write realistic production-style code, but do not add features the phase file doesn't ask for.

### Atlas safety rule (applies from the Atlas phases onward)
16. Atlas is **read-only**. Never write code that lets an agent or tool change the monitored server:
    no INSERT, UPDATE, DELETE, DDL, KILL, or configuration changes through any agent tool. Agents
    investigate and recommend; the human fixes.

### Record keeping
17. After each file is accepted, add one line to `docs/build-log.md`:
    `- <date> | <phase> | <file path> | <one-line purpose>`
