-- Execute manually in the Supabase SQL Editor when you decide to prepare a project.
-- The application does not create this table and does not connect during startup.
create table if not exists public.lmf_json_state (
  id text primary key,
  payload jsonb not null,
  schema_revision text not null,
  updated_at timestamptz not null default now()
);

alter table public.lmf_json_state enable row level security;
revoke all on table public.lmf_json_state from anon, authenticated;
grant select, insert, update, delete on table public.lmf_json_state to service_role;
