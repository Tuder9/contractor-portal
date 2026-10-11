create extension if not exists pgcrypto with schema extensions;

create table if not exists public.workspaces (
    id uuid primary key default gen_random_uuid(),
    name text not null check (length(trim(name)) between 1 and 120),
    created_at timestamptz not null default now()
);

create table if not exists public.contractors (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    contractor_name text not null,
    specialty text not null,
    project_assignment text not null default '',
    contract_value numeric(14, 2) not null check (contract_value >= 0),
    amount_paid numeric(14, 2) not null check (amount_paid >= 0 and amount_paid <= contract_value),
    start_date date not null,
    end_date date not null,
    status text not null check (status in ('Pending', 'Active', 'Completed')),
    phone_number text not null default '',
    created_at timestamptz not null default now()
);

alter table public.contractors
    add column if not exists phone_number text not null default '';

create index if not exists contractors_workspace_created_idx
    on public.contractors (workspace_id, created_at desc);

create table if not exists public.attendance_logs (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    contractor_id uuid not null references public.contractors(id) on delete cascade,
    attendance_date date not null,
    attendance_status text not null
        check (attendance_status in ('Present', 'Absent', 'Leave')),
    notes text not null default '',
    recorded_by uuid not null references auth.users(id),
    created_at timestamptz not null default now(),
    unique (contractor_id, attendance_date)
);

create index if not exists attendance_workspace_date_idx
    on public.attendance_logs (workspace_id, attendance_date desc);

create table if not exists public.memberships (
    user_id uuid primary key references auth.users(id) on delete cascade,
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    contractor_id uuid references public.contractors(id),
    email text not null,
    full_name text not null,
    role text not null check (role in ('Admin', 'Finance', 'Contractor', 'Viewer')),
    created_at timestamptz not null default now(),
    check ((role = 'Contractor' and contractor_id is not null) or
           (role <> 'Contractor' and contractor_id is null))
);

create table if not exists public.workspace_invites (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    token_hash text not null unique,
    invited_email text not null,
    role text not null check (role in ('Finance', 'Contractor', 'Viewer')),
    contractor_id uuid references public.contractors(id),
    expires_at timestamptz not null,
    redeemed_at timestamptz,
    check ((role = 'Contractor' and contractor_id is not null) or
           (role <> 'Contractor' and contractor_id is null))
);

create or replace function public.my_workspace_id()
returns uuid
language sql
stable
security definer
set search_path = public
as $$
    select workspace_id from public.memberships where user_id = auth.uid()
$$;

create or replace function public.my_role()
returns text
language sql
stable
security definer
set search_path = public
as $$
    select role from public.memberships where user_id = auth.uid()
$$;

drop function if exists public.get_contractors();
create function public.get_contractors()
returns table (
    id uuid,
    contractor_name text,
    specialty text,
    project_assignment text,
    phone_number text,
    contract_value numeric,
    amount_paid numeric,
    start_date date,
    end_date date,
    status text,
    created_at timestamptz
)
language plpgsql
stable
security definer
set search_path = public
as $$
begin
    if public.my_workspace_id() is null then
        raise exception 'Authentication and workspace membership are required';
    end if;

    return query
    select c.id, c.contractor_name, c.specialty, c.project_assignment, c.phone_number,
           case when public.my_role() in ('Admin', 'Finance') then c.contract_value end,
           case when public.my_role() in ('Admin', 'Finance') then c.amount_paid end,
           c.start_date, c.end_date, c.status, c.created_at
    from public.contractors c
    where c.workspace_id = public.my_workspace_id()
      and (
          public.my_role() <> 'Contractor'
          or c.id = (
              select m.contractor_id
              from public.memberships m
              where m.user_id = auth.uid()
          )
      )
    order by c.created_at desc;
end;
$$;

drop function if exists public.get_my_membership();
create function public.get_my_membership()
returns table (workspace_name text, workspace_id uuid, email text, role text)
language sql
stable
security definer
set search_path = public
as $$
    select w.name, w.id, m.email, m.role
    from public.memberships m
    join public.workspaces w on w.id = m.workspace_id
    where m.user_id = auth.uid()
$$;

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
    invite text := nullif(trim(new.raw_user_meta_data ->> 'invite_code'), '');
    workspace_id uuid;
    contractor_id uuid;
    assigned_role text;
    workspace_name text := coalesce(
        nullif(trim(new.raw_user_meta_data ->> 'company_name'), ''),
        'My Company'
    );
    full_name text := coalesce(
        nullif(trim(new.raw_user_meta_data ->> 'full_name'), ''),
        'Company Member'
    );
begin
    if invite is not null then
        select i.workspace_id, i.role, i.contractor_id
        into workspace_id, assigned_role, contractor_id
        from public.workspace_invites i
        where i.token_hash = encode(extensions.digest(invite, 'sha256'), 'hex')
          and lower(i.invited_email) = lower(new.email)
          and i.redeemed_at is null
          and i.expires_at > now()
        for update;

        if workspace_id is null then
            raise exception 'Invalid or expired company invite code';
        end if;

        update public.workspace_invites
        set redeemed_at = now()
        where token_hash = encode(extensions.digest(invite, 'sha256'), 'hex');
    else
        insert into public.workspaces (name)
        values (workspace_name)
        returning id into workspace_id;
        assigned_role := 'Admin';
    end if;

    insert into public.memberships (
        user_id, workspace_id, contractor_id, email, full_name, role
    )
    values (
        new.id, workspace_id, contractor_id, lower(new.email), full_name, assigned_role
    );
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

drop function if exists public.create_workspace_invite(text);
create or replace function public.create_workspace_invite(
    requested_role text,
    invited_email text,
    target_contractor_id uuid default null
)
returns text
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
    workspace_id uuid;
    invite_token text := encode(extensions.gen_random_bytes(32), 'hex');
begin
    if public.my_role() is distinct from 'Admin' then
        raise exception 'Only workspace admins can create invites';
    end if;
    if requested_role not in ('Finance', 'Contractor', 'Viewer') then
        raise exception 'Invalid invite role';
    end if;
    if nullif(trim(invited_email), '') is null then
        raise exception 'An invite email address is required';
    end if;

    workspace_id := public.my_workspace_id();
    if requested_role = 'Contractor' and not exists (
        select 1 from public.contractors c
        where c.id = target_contractor_id
          and c.workspace_id = public.my_workspace_id()
    ) then
        raise exception 'Choose a contractor record in this workspace';
    end if;

    insert into public.workspace_invites (
        workspace_id, token_hash, invited_email, role, contractor_id, expires_at
    )
    values (
        workspace_id,
        encode(extensions.digest(invite_token, 'sha256'), 'hex'),
        lower(trim(invited_email)),
        requested_role,
        target_contractor_id,
        now() + interval '7 days'
    );
    return invite_token;
end;
$$;

drop function if exists public.create_contractor(text, text, text, numeric, numeric, date, date, text);
create function public.create_contractor(
    new_name text,
    new_specialty text,
    new_project text,
    new_phone text,
    new_value numeric,
    new_paid numeric,
    new_start_date date,
    new_end_date date,
    new_status text
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    new_id uuid;
begin
    if public.my_role() is distinct from 'Admin' then
        raise exception 'Only workspace admins can add contractor records';
    end if;
    if trim(new_name) = '' or trim(new_specialty) = '' then
        raise exception 'Contractor name and specialty are required';
    end if;
    if new_paid < 0 or new_value < new_paid then
        raise exception 'Amount paid must be between zero and contract value';
    end if;

    insert into public.contractors (
        workspace_id, contractor_name, specialty, project_assignment,
        phone_number, contract_value, amount_paid, start_date, end_date, status
    )
    values (
        public.my_workspace_id(), trim(new_name), trim(new_specialty),
        coalesce(trim(new_project), ''), coalesce(trim(new_phone), ''),
        new_value, new_paid,
        new_start_date, new_end_date, new_status
    )
    returning id into new_id;
    return new_id;
end;
$$;

drop function if exists public.edit_contractor(uuid, text, text, text, numeric, numeric, date, date, text);
create function public.edit_contractor(
    target_id uuid,
    new_name text,
    new_specialty text,
    new_project text,
    new_phone text,
    new_value numeric,
    new_paid numeric,
    new_start_date date,
    new_end_date date,
    new_status text
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if public.my_role() is distinct from 'Admin' then
        raise exception 'Only workspace admins can edit contractor records';
    end if;
    if trim(new_name) = '' or trim(new_specialty) = '' then
        raise exception 'Contractor name and specialty are required';
    end if;
    if new_paid < 0 or new_value < new_paid then
        raise exception 'Amount paid must be between zero and contract value';
    end if;
    if new_status not in ('Pending', 'Active', 'Completed') then
        raise exception 'Invalid contractor status';
    end if;

    update public.contractors
    set contractor_name = trim(new_name),
        specialty = trim(new_specialty),
        project_assignment = coalesce(trim(new_project), ''),
        phone_number = coalesce(trim(new_phone), ''),
        contract_value = new_value,
        amount_paid = new_paid,
        start_date = new_start_date,
        end_date = new_end_date,
        status = new_status
    where id = target_id and workspace_id = public.my_workspace_id();

    if not found then
        raise exception 'Contractor record not found in this workspace';
    end if;
end;
$$;

create or replace function public.update_contractor_payment(
    target_id uuid,
    new_paid numeric
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if coalesce(public.my_role(), '') not in ('Admin', 'Finance') then
        raise exception 'Finance role required to update payments';
    end if;
    if new_paid < 0 then
        raise exception 'Payment cannot be negative';
    end if;

    update public.contractors
    set amount_paid = new_paid
    where id = target_id
      and workspace_id = public.my_workspace_id()
      and new_paid <= contract_value;

    if not found then
        raise exception 'Record not found or payment exceeds contract value';
    end if;
end;
$$;

create or replace function public.update_contractor_status(
    target_id uuid,
    new_status text
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
    assigned_contractor_id uuid;
begin
    if coalesce(public.my_role(), '') not in ('Admin', 'Contractor') then
        raise exception 'Contractor role required to update project status';
    end if;
    if new_status not in ('Pending', 'Active', 'Completed') then
        raise exception 'Invalid contractor status';
    end if;

    if public.my_role() = 'Contractor' then
        select m.contractor_id into assigned_contractor_id
        from public.memberships m
        where m.user_id = auth.uid();
        if target_id <> assigned_contractor_id then
            raise exception 'Contractor accounts can only update their assigned record';
        end if;
    end if;

    update public.contractors
    set status = new_status
    where id = target_id and workspace_id = public.my_workspace_id();

    if not found then
        raise exception 'Contractor record not found in this workspace';
    end if;
end;
$$;

create or replace function public.get_attendance(
    start_date date,
    end_date date
)
returns table (
    contractor_id uuid,
    contractor_name text,
    phone_number text,
    attendance_date date,
    attendance_status text,
    notes text
)
language plpgsql
stable
security definer
set search_path = public
as $$
begin
    if coalesce(public.my_role(), '') not in ('Admin', 'Finance', 'Contractor', 'Viewer') then
        raise exception 'Authentication and workspace membership are required';
    end if;
    if end_date < start_date or end_date - start_date > 366 then
        raise exception 'Attendance date range must be valid and no longer than one year';
    end if;

    return query
    select c.id, c.contractor_name, c.phone_number,
           a.attendance_date, a.attendance_status, a.notes
    from public.attendance_logs a
    join public.contractors c on c.id = a.contractor_id
    where a.workspace_id = public.my_workspace_id()
      and a.attendance_date between start_date and end_date
      and (
          public.my_role() <> 'Contractor'
          or c.id = (
              select m.contractor_id
              from public.memberships m
              where m.user_id = auth.uid()
          )
      )
    order by a.attendance_date desc, c.contractor_name;
end;
$$;

create or replace function public.mark_attendance(
    target_contractor_id uuid,
    work_date date,
    new_status text,
    new_notes text default ''
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
    assigned_contractor_id uuid;
begin
    if coalesce(public.my_role(), '') not in ('Admin', 'Contractor') then
        raise exception 'Only admins and assigned contractors can record attendance';
    end if;
    if new_status not in ('Present', 'Absent', 'Leave') then
        raise exception 'Invalid attendance status';
    end if;
    if work_date > current_date or work_date < current_date - 366 then
        raise exception 'Attendance date must be within the last year and not in the future';
    end if;

    if public.my_role() = 'Contractor' then
        select m.contractor_id into assigned_contractor_id
        from public.memberships m
        where m.user_id = auth.uid();
        if target_contractor_id <> assigned_contractor_id then
            raise exception 'Contractor accounts can only record their own attendance';
        end if;
    end if;

    insert into public.attendance_logs (
        workspace_id, contractor_id, attendance_date, attendance_status, notes, recorded_by
    )
    select public.my_workspace_id(), c.id, work_date, new_status, coalesce(trim(new_notes), ''),
           auth.uid()
    from public.contractors c
    where c.id = target_contractor_id
      and c.workspace_id = public.my_workspace_id()
    on conflict (contractor_id, attendance_date) do update
        set attendance_status = excluded.attendance_status,
            notes = excluded.notes,
            recorded_by = excluded.recorded_by;

    if not found then
        raise exception 'Contractor record not found in this workspace';
    end if;
end;
$$;

alter table public.workspaces enable row level security;
alter table public.memberships enable row level security;
alter table public.workspace_invites enable row level security;
alter table public.contractors enable row level security;
alter table public.attendance_logs enable row level security;

drop policy if exists workspace_read_members on public.workspaces;
create policy workspace_read_members on public.workspaces
    for select to authenticated using (id = public.my_workspace_id());
drop policy if exists membership_read_members on public.memberships;
create policy membership_read_members on public.memberships
    for select to authenticated using (workspace_id = public.my_workspace_id());
drop policy if exists contractor_read_members on public.contractors;
create policy contractor_read_members on public.contractors
    for select to authenticated using (workspace_id = public.my_workspace_id());

revoke all on public.workspaces, public.memberships, public.workspace_invites,
    public.contractors, public.attendance_logs from anon, authenticated;

revoke all on function public.my_workspace_id() from public, anon;
revoke all on function public.my_role() from public, anon;
revoke all on function public.get_my_membership() from public, anon;
revoke all on function public.get_contractors() from public, anon;
revoke all on function public.get_attendance(date, date) from public, anon;
revoke all on function public.mark_attendance(uuid, date, text, text) from public, anon;
revoke all on function public.create_workspace_invite(text, text, uuid) from public, anon;
revoke all on function public.create_contractor(text, text, text, text, numeric, numeric, date, date, text) from public, anon;
revoke all on function public.edit_contractor(uuid, text, text, text, text, numeric, numeric, date, date, text) from public, anon;
revoke all on function public.update_contractor_payment(uuid, numeric) from public, anon;
revoke all on function public.update_contractor_status(uuid, text) from public, anon;

grant execute on function public.my_workspace_id() to authenticated;
grant execute on function public.my_role() to authenticated;
grant execute on function public.get_contractors() to authenticated;
grant execute on function public.get_attendance(date, date) to authenticated;
grant execute on function public.mark_attendance(uuid, date, text, text) to authenticated;
grant execute on function public.get_my_membership() to authenticated;
grant execute on function public.create_workspace_invite(text, text, uuid) to authenticated;
grant execute on function public.create_contractor(text, text, text, text, numeric, numeric, date, date, text) to authenticated;
grant execute on function public.edit_contractor(uuid, text, text, text, text, numeric, numeric, date, date, text) to authenticated;
grant execute on function public.update_contractor_payment(uuid, numeric) to authenticated;
grant execute on function public.update_contractor_status(uuid, text) to authenticated;

revoke all on function public.handle_new_user() from public, anon, authenticated;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'contractor-photos',
    'contractor-photos',
    false,
    10485760,
    array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update set
    public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists contractor_photos_read on storage.objects;
create policy contractor_photos_read on storage.objects
    for select to authenticated
    using (
        bucket_id = 'contractor-photos'
        and split_part(name, '/', 1) = public.my_workspace_id()::text
        and (
            public.my_role() in ('Admin', 'Finance', 'Viewer')
            or (
                public.my_role() = 'Contractor'
                and split_part(name, '/', 2) = (
                    select m.contractor_id::text
                    from public.memberships m
                    where m.user_id = auth.uid()
                )
            )
        )
    );

drop policy if exists contractor_photos_upload on storage.objects;
create policy contractor_photos_upload on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'contractor-photos'
        and split_part(name, '/', 1) = public.my_workspace_id()::text
        and exists (
            select 1
            from public.contractors c
            where c.id::text = split_part(name, '/', 2)
              and c.workspace_id = public.my_workspace_id()
              and (
                  public.my_role() = 'Admin'
                  or (
                      public.my_role() = 'Contractor'
                      and c.id = (
                          select m.contractor_id
                          from public.memberships m
                          where m.user_id = auth.uid()
                      )
                  )
              )
        )
    );

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'contractor-files',
    'contractor-files',
    false,
    15728640,
    array[
        'application/pdf',
        'image/jpeg',
        'image/png',
        'image/webp',
        'text/plain'
    ]
)
on conflict (id) do update set
    public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists contractor_files_read on storage.objects;
create policy contractor_files_read on storage.objects
    for select to authenticated
    using (
        bucket_id = 'contractor-files'
        and split_part(name, '/', 1) = public.my_workspace_id()::text
        and (
            public.my_role() in ('Admin', 'Finance', 'Viewer')
            or (
                public.my_role() = 'Contractor'
                and split_part(name, '/', 2) = (
                    select m.contractor_id::text
                    from public.memberships m
                    where m.user_id = auth.uid()
                )
            )
        )
    );

drop policy if exists contractor_files_upload on storage.objects;
create policy contractor_files_upload on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'contractor-files'
        and split_part(name, '/', 1) = public.my_workspace_id()::text
        and exists (
            select 1 from public.contractors c
            where c.id::text = split_part(name, '/', 2)
              and c.workspace_id = public.my_workspace_id()
              and (
                  public.my_role() = 'Admin'
                  or (
                      public.my_role() = 'Contractor'
                      and c.id = (
                          select m.contractor_id
                          from public.memberships m
                          where m.user_id = auth.uid()
                      )
                  )
              )
        )
    );
