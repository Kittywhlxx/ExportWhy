-- The metastore database comes from POSTGRES_DB; the audit log needs its own.
--
-- Scripts in /docker-entrypoint-initdb.d run only when PGDATA is empty, so this is a
-- first-boot action. Both schemas are created by Flyway on first connection from the
-- driver and the API respectively -- nothing here creates tables.
CREATE DATABASE audit;
