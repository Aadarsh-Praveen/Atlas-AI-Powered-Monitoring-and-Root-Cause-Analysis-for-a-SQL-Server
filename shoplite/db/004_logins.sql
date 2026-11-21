/*
    004_logins.sql
    Creates the shoplite_app login and database user used by the ShopLite API,
    with least-privilege permissions: read and write data in dbo, execute
    stored procedures in dbo, and nothing else.

    The password is never stored in this file. $(SHOPLITE_APP_PASSWORD) is a
    placeholder (sqlcmd variable syntax) that apply_schema.py replaces with the
    value from the root .env just before running the script.

    Logins vs. users:
      - A LOGIN is server-level (stored in master). It lets you connect.
      - A USER is database-level. It maps a login into one database, where
        permissions are granted.
    SalesDB is dropped and recreated by 001, which deletes the user but NOT
    the login. So the login is created only if missing, and its password is
    reset each run so it always matches .env.
*/

USE master;
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'shoplite_app')
BEGIN
    CREATE LOGIN shoplite_app WITH
        PASSWORD = N'$(SHOPLITE_APP_PASSWORD)',
        DEFAULT_DATABASE = SalesDB,
        CHECK_POLICY = ON,        -- enforce SQL Server's password complexity rules
        CHECK_EXPIRATION = OFF;   -- a service account's password shouldn't expire
                                  -- unannounced and take the app down
END
ELSE
BEGIN
    -- Already exists from an earlier run: bring it back in line with .env.
    ALTER LOGIN shoplite_app WITH
        PASSWORD = N'$(SHOPLITE_APP_PASSWORD)',
        DEFAULT_DATABASE = SalesDB,
        CHECK_POLICY = ON,
        CHECK_EXPIRATION = OFF;
    ALTER LOGIN shoplite_app ENABLE;
END;
GO

USE SalesDB;
GO

-- Map the login into SalesDB.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_app')
    CREATE USER shoplite_app FOR LOGIN shoplite_app WITH DEFAULT_SCHEMA = dbo;
GO

-- Permissions are granted to a role, not directly to the user. The role
-- documents in one place what "the app" may do, and a second app user
-- (or a replacement) can simply be added to it.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_app_role' AND type = 'R')
    CREATE ROLE shoplite_app_role;
GO

-- Schema-level grants cover every current and future table and procedure in
-- dbo. Nothing is granted on the vendor schema: the API doesn't use it.
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::dbo TO shoplite_app_role;  -- read and write data
GRANT EXECUTE ON SCHEMA::dbo TO shoplite_app_role;                         -- run the report procedures
GO

ALTER ROLE shoplite_app_role ADD MEMBER shoplite_app;
GO

/*
    What shoplite_app can NOT do, because it was never granted:
      - Create, alter, or drop tables, indexes, or procedures (no CREATE/ALTER
        permissions, not in db_owner or db_ddladmin).
      - Read or change the vendor schema.
      - Change database or server settings, create logins, or KILL sessions.
      - Read server-wide DMVs (no VIEW SERVER STATE).
    Part C1 step 6 checks this: log in as shoplite_app and try CREATE TABLE.
*/
