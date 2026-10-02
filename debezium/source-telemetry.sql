GRANT SELECT ON public.telemetry, public.export_checkpoint TO debezium;
ALTER TABLE public.telemetry REPLICA IDENTITY FULL;
ALTER TABLE public.export_checkpoint REPLICA IDENTITY FULL;
SELECT 'CREATE PUBLICATION bionicpro_cdc FOR TABLE public.telemetry, public.export_checkpoint, public.cdc_control'
WHERE NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname='bionicpro_cdc') \gexec
