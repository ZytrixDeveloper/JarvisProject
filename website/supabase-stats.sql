-- Jarvis live stats: run once in Supabase -> SQL Editor -> New query -> Run.
-- Adds an approximate location to each linked PC and a public, anonymous
-- summary (counts per country only, no user IDs, no exact places).

alter table public.devices
  add column if not exists country text,
  add column if not exists lat real,
  add column if not exists lon real;

create or replace function public.jarvis_stats()
returns json
language sql
security definer
set search_path = public
stable
as $$
  select json_build_object(
    'online',    (select count(*) from devices where last_seen > now() - interval '7 minutes'),
    'devices',   (select count(*) from devices),
    'users',     (select count(distinct user_id) from devices),
    'countries', (select count(distinct country) from devices where country is not null),
    'places', coalesce((
      select json_agg(p) from (
        select country,
               round(avg(lat)::numeric, 0) as lat,
               round(avg(lon)::numeric, 0) as lon,
               count(*) as users,
               count(*) filter (where last_seen > now() - interval '7 minutes') as online
        from devices
        where lat is not null and country is not null
        group by country
      ) p), '[]'::json)
  );
$$;

revoke all on function public.jarvis_stats() from public;
grant execute on function public.jarvis_stats() to anon, authenticated;
