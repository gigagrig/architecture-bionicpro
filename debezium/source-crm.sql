-- The existing exclusion constraint already forbids duplicate keys.
SELECT 'ALTER TABLE public.ownership ADD PRIMARY KEY (prosthesis_id, valid_from)'
WHERE NOT EXISTS (SELECT 1 FROM pg_constraint
    WHERE conrelid='public.ownership'::regclass AND contype='p') \gexec
GRANT SELECT ON public.customers, public.prostheses, public.ownership, public.export_checkpoint TO debezium;
ALTER TABLE public.customers REPLICA IDENTITY FULL;
ALTER TABLE public.prostheses REPLICA IDENTITY FULL;
ALTER TABLE public.ownership REPLICA IDENTITY FULL;
ALTER TABLE public.export_checkpoint REPLICA IDENTITY FULL;
SELECT 'CREATE PUBLICATION bionicpro_cdc FOR TABLE public.customers, public.prostheses, public.ownership, public.export_checkpoint, public.cdc_control'
WHERE NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname='bionicpro_cdc') \gexec
