"""Idempotent T-SQL database and login provisioning for Microsoft SQL Server 2022."""

from collections.abc import Iterable
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Set

from ignition_gateway_harness.core.inspector import GatewaySpec

logger = logging.getLogger(__name__)

STANDARD_HISTORIAN_TABLES_SQL = """
IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = N'sqlth_drv')
BEGIN
    CREATE TABLE [dbo].[sqlth_drv] (
        [id] INT IDENTITY(1,1) PRIMARY KEY,
        [name] VARCHAR(255) NOT NULL UNIQUE
    );
    INSERT INTO [dbo].[sqlth_drv] ([name]) VALUES ('default');
END
GO

IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = N'sqlth_tables')
BEGIN
    CREATE TABLE [dbo].[sqlth_tables] (
        [id] INT IDENTITY(1,1) PRIMARY KEY,
        [tablename] VARCHAR(255) NOT NULL UNIQUE,
        [drvid] INT FOREIGN KEY REFERENCES [dbo].[sqlth_drv]([id])
    );
END
GO

IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = N'sqlth_te')
BEGIN
    CREATE TABLE [dbo].[sqlth_te] (
        [id] INT IDENTITY(1,1) PRIMARY KEY,
        [tagid] VARCHAR(500) NOT NULL UNIQUE,
        [datatype] INT NOT NULL,
        [querymode] INT NOT NULL
    );
END
GO

IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = N'sqlth_sce')
BEGIN
    CREATE TABLE [dbo].[sqlth_sce] (
        [id] INT IDENTITY(1,1) PRIMARY KEY,
        [scid] INT NOT NULL,
        [start_time] BIGINT NOT NULL,
        [end_time] BIGINT NULL
    );
END
GO

IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = N'alarm_events')
BEGIN
    CREATE TABLE [dbo].[alarm_events] (
        [id] INT IDENTITY(1,1) PRIMARY KEY,
        [eventid] VARCHAR(255) NOT NULL,
        [source] VARCHAR(1000) NOT NULL,
        [displaypath] VARCHAR(1000) NULL,
        [priority] INT NULL,
        [eventtime] DATETIME NULL,
        [eventflags] INT NULL
    );
END
GO

IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = N'alarm_event_data')
BEGIN
    CREATE TABLE [dbo].[alarm_event_data] (
        [id] INT IDENTITY(1,1) PRIMARY KEY,
        [eventid] VARCHAR(255) NOT NULL,
        [propname] VARCHAR(255) NOT NULL,
        [propval_str] NVARCHAR(MAX) NULL,
        [propval_int] BIGINT NULL,
        [propval_float] FLOAT NULL
    );
END
GO

IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = N'audit_events')
BEGIN
    CREATE TABLE [dbo].[audit_events] (
        [audit_events_id] INT IDENTITY(1,1) PRIMARY KEY,
        [event_timestamp] DATETIME NOT NULL DEFAULT GETDATE(),
        [actor] NVARCHAR(255) NOT NULL,
        [actor_host] NVARCHAR(255) NULL,
        [action] NVARCHAR(255) NOT NULL,
        [action_target] NVARCHAR(255) NULL,
        [action_value] NVARCHAR(MAX) NULL,
        [status_code] INT NOT NULL DEFAULT 0
    );
END
GO

IF NOT EXISTS (SELECT * FROM sys.objects WHERE name = N'AUDIT_EVENTS')
BEGIN
    CREATE SYNONYM [dbo].[AUDIT_EVENTS] FOR [dbo].[audit_events];
END
GO
"""


class DatabaseProvisioner:
    """Idempotently provisions MSSQL databases, logins, and schema tables."""

    def __init__(self, default_password: str = "password"):
        self.default_password = default_password

    def generate_provision_sql(
        self,
        databases: Iterable[str],
        users: Iterable[str],
        include_historian_schema: bool = True,
    ) -> str:
        """Generate idempotent T-SQL script for MSSQL 2022.

        Enforces CHECK_POLICY = OFF and CHECK_EXPIRATION = OFF for all logins.
        """
        clean_dbs: Set[str] = set()
        for d in databases:
            c = d.lower().replace("-", "_").strip()
            if c and c not in ("master", "tempdb", "model", "msdb"):
                clean_dbs.add(c)

        clean_users: Set[str] = set()
        for u in users:
            c = u.strip()
            if c and c.lower() not in ("sa", "dbo"):
                clean_users.add(c)
        clean_users.add("ignition")

        lines = [
            "USE [master];",
            "GO",
            "",
            "-- 1. Idempotent Database Creation",
        ]
        for db in sorted(clean_dbs):
            lines.extend([
                f"IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = N'{db}')",
                "BEGIN",
                f"    PRINT 'Creating database [{db}] with collation SQL_Latin1_General_CP1_CI_AS...';",
                f"    CREATE DATABASE [{db}] COLLATE SQL_Latin1_General_CP1_CI_AS;",
                f"    ALTER DATABASE [{db}] SET RECOVERY SIMPLE;",
                "END",
                "GO",
                "",
            ])

        lines.extend([
            "-- 2. Configure sa and Application Logins",
            f"ALTER LOGIN [sa] WITH PASSWORD = N'{self.default_password}', CHECK_POLICY = OFF;",
            "GO",
            "",
        ])
        for u in sorted(clean_users):
            lines.extend([
                f"IF NOT EXISTS (SELECT name FROM sys.server_principals WHERE name = N'{u}')",
                "BEGIN",
                f"    PRINT 'Creating server login [{u}]...';",
                f"    CREATE LOGIN [{u}] WITH PASSWORD = N'{self.default_password}',",
                "        DEFAULT_DATABASE = [master],",
                "        CHECK_EXPIRATION = OFF,",
                "        CHECK_POLICY = OFF;",
                "END",
                "ELSE",
                "BEGIN",
                f"    ALTER LOGIN [{u}] WITH PASSWORD = N'{self.default_password}',",
                "        CHECK_EXPIRATION = OFF,",
                "        CHECK_POLICY = OFF;",
                "END",
                "GO",
                "",
            ])

        lines.extend([
            "-- 3. Map Users and Grant db_owner Privileges",
        ])
        for db in sorted(clean_dbs):
            lines.extend([
                f"USE [{db}];",
                "GO",
            ])
            for u in sorted(clean_users):
                lines.extend([
                    f"IF NOT EXISTS (SELECT name FROM sys.database_principals WHERE name = N'{u}')",
                    "BEGIN",
                    f"    CREATE USER [{u}] FOR LOGIN [{u}];",
                    "END",
                    f"ALTER ROLE [db_owner] ADD MEMBER [{u}];",
                    "GO",
                ])
            lines.append("")

            if include_historian_schema:
                lines.extend([
                    f"-- Schema tables for [{db}]",
                    f"USE [{db}];",
                    "GO",
                    STANDARD_HISTORIAN_TABLES_SQL.strip(),
                    "",
                ])

        return "\n".join(lines)

    def execute_tsql(
        self,
        query: str,
        container_name: str = "sim-mssql",
        timeout_secs: int = 30,
    ) -> bool:
        """Execute a T-SQL query inside running MSSQL container via sqlcmd with STDIN piping and error detection."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            logger.debug("Docker not available in PATH; skipping T-SQL execution")
            return False

        passwords_to_try = [self.default_password]
        env_pwd = os.environ.get("MSSQL_SA_PASSWORD")
        if env_pwd and env_pwd not in passwords_to_try:
            passwords_to_try.append(env_pwd)
        if "Password123!" not in passwords_to_try:
            passwords_to_try.append("Password123!")

        tools_paths = [
            ("/opt/mssql-tools18/bin/sqlcmd", ["-C", "-b"]),
            ("/opt/mssql-tools/bin/sqlcmd", ["-b"]),
        ]

        for pwd in passwords_to_try:
            for sqlcmd_bin, extra_flags in tools_paths:
                cmd = [
                    docker_bin,
                    "exec",
                    "-i",
                    container_name,
                    sqlcmd_bin,
                    "-S",
                    "localhost",
                    "-U",
                    "sa",
                    "-P",
                    pwd,
                ] + extra_flags

                try:
                    res = subprocess.run(
                        cmd,
                        input=query,
                        capture_output=True,
                        text=True,
                        timeout=timeout_secs,
                        check=False,
                    )
                    if res.returncode == 0:
                        logger.info("Successfully executed T-SQL script in %s via %s", container_name, sqlcmd_bin)
                        return True
                    else:
                        logger.debug(
                            "sqlcmd attempt failed (%s, code %d): stderr=%s stdout=%s",
                            sqlcmd_bin,
                            res.returncode,
                            res.stderr.strip() if res.stderr else "",
                            res.stdout.strip() if res.stdout else "",
                        )
                except Exception as exc:
                    logger.debug("Error invoking sqlcmd (%s): %s", sqlcmd_bin, exc)

        logger.warning(
            "Failed executing T-SQL in container %s across all sqlcmd paths and credentials",
            container_name,
        )
        return False

    def ensure_database_and_login(
        self,
        database: str,
        username: Optional[str] = None,
        container_name: str = "sim-mssql",
    ) -> bool:
        """Idempotently ensure a specific database and user login exist in the running MSSQL stack."""
        clean_db = database.lower().replace("-", "_").strip()
        users = [username.strip()] if username and username.strip() else ["ignition"]
        sql = self.generate_provision_sql([clean_db], users, include_historian_schema=True)
        return self.execute_tsql(sql, container_name=container_name)

    def provision_for_spec(
        self,
        spec: GatewaySpec,
        container_name: str = "sim-mssql",
    ) -> bool:
        """Idempotently provision all databases and logins discovered in a GatewaySpec."""
        if not spec.database_names and not spec.database_users:
            return True
        sql = self.generate_provision_sql(
            spec.database_names,
            spec.database_users,
            include_historian_schema=True,
        )
        return self.execute_tsql(sql, container_name=container_name)
