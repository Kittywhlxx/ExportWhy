-- Three independent databases in one Postgres server: Zetaris registers each as its own datasource.
-- Everything is synthetic (data/gen_data.py).
CREATE DATABASE logsdb;
CREATE DATABASE policydb;
CREATE DATABASE ticketsdb;

\connect logsdb
CREATE TABLE accounts (account_id text PRIMARY KEY, display_name text, role text, department text, manager_id text, status text, status_since timestamp, email text, phone text);
CREATE TABLE export_events (event_id text PRIMARY KEY, ts timestamp, account_id text, dataset_id text, rows_exported integer, destination_type text, destination text, client_ip text, tool text, ticket_ref text, sample_record_ids text);
CREATE TABLE alerts (alert_id text PRIMARY KEY, event_id text, raised_at timestamp, rule text, severity text, status text);
\copy accounts FROM '/seed/accounts.csv' WITH (FORMAT csv, HEADER true)
\copy export_events FROM '/seed/export_events.csv' WITH (FORMAT csv, HEADER true)
\copy alerts FROM '/seed/alerts.csv' WITH (FORMAT csv, HEADER true)

\connect policydb
CREATE TABLE datasets (dataset_id text PRIMARY KEY, name text, sensitivity text, owner_team text, record_count integer, contains_pii boolean);
CREATE TABLE policies (policy_id text PRIMARY KEY, role text, dataset_id text, allowed boolean, allowed_hours_start text, allowed_hours_end text, max_rows_without_ticket integer, requires_ticket boolean, notes text);
CREATE TABLE policy_exceptions (exception_id text PRIMARY KEY, scope_type text, scope_value text, dataset_id text, clause text, valid_from timestamp, valid_to timestamp, approved_by text);
CREATE TABLE field_sensitivity (table_name text, column_name text, level text, PRIMARY KEY (table_name, column_name));
\copy datasets FROM '/seed/datasets.csv' WITH (FORMAT csv, HEADER true)
\copy policies FROM '/seed/policies.csv' WITH (FORMAT csv, HEADER true)
\copy policy_exceptions FROM '/seed/policy_exceptions.csv' WITH (FORMAT csv, HEADER true)
\copy field_sensitivity FROM '/seed/field_sensitivity.csv' WITH (FORMAT csv, HEADER true)

\connect ticketsdb
CREATE TABLE tickets (ticket_id text PRIMARY KEY, requester_account_id text, dataset_id text, purpose text, max_rows integer, valid_from timestamp, valid_to timestamp, status text, approver_id text, created_at timestamp);
\copy tickets FROM '/seed/tickets.csv' WITH (FORMAT csv, HEADER true)
