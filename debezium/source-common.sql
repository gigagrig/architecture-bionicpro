\getenv cdc_password CDC_PASSWORD
SELECT 'CREATE ROLE debezium LOGIN REPLICATION'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='debezium') \gexec
ALTER ROLE debezium WITH LOGIN REPLICATION PASSWORD :'cdc_password';
SELECT format('GRANT CONNECT ON DATABASE %I TO debezium', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO debezium;
CREATE TABLE IF NOT EXISTS public.cdc_control (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    touched_at TIMESTAMPTZ NOT NULL
);
INSERT INTO public.cdc_control VALUES (TRUE, '1970-01-01') ON CONFLICT DO NOTHING;
GRANT SELECT, UPDATE ON public.cdc_control TO debezium;
ALTER TABLE public.cdc_control REPLICA IDENTITY FULL;
