-- Sayproof: storage for hosted speech input. Run once in the Supabase SQL editor (or via the Management API).
-- The browser uploads through signed URLs made with the secret key; nothing here is readable with the public key.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('recordings', 'recordings', false, 26214400, array['audio/*', 'video/*'])
on conflict (id) do nothing;

create table if not exists public.checks (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  audio_path text,
  transcript text not null,
  receipt jsonb,
  asr_model text
);
alter table public.checks enable row level security;
revoke all on public.checks from anon, authenticated;
