-- Jarvis: run this once in Supabase -> SQL Editor -> New query -> Run.
-- Creates the "devices" table: one row per PC linked to an account.

create table if not exists public.devices (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  device_id text not null,
  device_name text not null,
  app_version text,
  linked_at timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  unique (user_id, device_id)
);

alter table public.devices enable row level security;

grant select, insert, update, delete on public.devices to authenticated;

-- Each user can only see and change their own devices.
create policy "devices_select_own" on public.devices
  for select to authenticated using ((select auth.uid()) = user_id);
create policy "devices_insert_own" on public.devices
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "devices_update_own" on public.devices
  for update to authenticated using ((select auth.uid()) = user_id);
create policy "devices_delete_own" on public.devices
  for delete to authenticated using ((select auth.uid()) = user_id);
