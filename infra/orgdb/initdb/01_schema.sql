-- Synthetic internal dependency inventory for the DepRisk demo. No real people or systems.
CREATE TABLE services (
  service text PRIMARY KEY,
  owner_team text NOT NULL,
  tier integer NOT NULL,
  language text NOT NULL
);
CREATE TABLE dependencies (
  service text NOT NULL REFERENCES services(service),
  package text NOT NULL,
  pinned_version text NOT NULL,
  scope text NOT NULL,
  added_on date NOT NULL,
  PRIMARY KEY (service, package)
);
CREATE TABLE license_policy (
  license text PRIMARY KEY,
  decision text NOT NULL,
  note text
);
CREATE TABLE risk_exceptions (
  exception_id text PRIMARY KEY,
  service text NOT NULL,
  package text NOT NULL,
  advisory_id text NOT NULL,
  approved_by text NOT NULL,
  approved_on date NOT NULL,
  expires_on date NOT NULL,
  reason text
);
CREATE TABLE adoption_requests (
  request_id text PRIMARY KEY,
  service text NOT NULL,
  package text NOT NULL,
  requested_version text NOT NULL,
  requested_by text NOT NULL,
  requested_on date NOT NULL,
  reason text,
  status text NOT NULL
);
\copy services FROM '/seed/services.csv' WITH (FORMAT csv, HEADER true)
\copy dependencies FROM '/seed/dependencies.csv' WITH (FORMAT csv, HEADER true)
\copy license_policy FROM '/seed/license_policy.csv' WITH (FORMAT csv, HEADER true, NULL '')
\copy risk_exceptions FROM '/seed/risk_exceptions.csv' WITH (FORMAT csv, HEADER true)
\copy adoption_requests FROM '/seed/adoption_requests.csv' WITH (FORMAT csv, HEADER true)
