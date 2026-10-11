# Contractor Portal

A hosted, multi-company contractor management website built with Streamlit and Supabase.
Each company has an isolated workspace. Supabase Auth verifies email addresses and manages
passwords; the application never stores passwords. Database row-level security and guarded
database functions enforce workspace and role boundaries.

## Deploy worldwide

This project is prepared for Streamlit Community Cloud and Supabase. The source folder must
be pushed to a GitHub repository before Community Cloud can deploy it. Do not upload
`.streamlit/secrets.toml`, database exports, or credentials to GitHub.

1. Create a Supabase project and run [`supabase_schema.sql`](./supabase_schema.sql) in its
   SQL Editor. The schema is safe to re-run when applying later updates.
2. In Supabase Authentication settings, keep email confirmation enabled. Direct signup does
   not need another admin's approval: a new user verifies their email and owns a private
   workspace. Set the Site URL
   and allowed redirect URL to the Streamlit app URL after deployment. Configure SMTP if
   you need reliable verification email delivery at scale. The SQL schema creates private
   photo and document buckets with workspace-scoped access policies.
3. Push this project folder to a private GitHub repository.
4. In Streamlit Community Cloud, deploy `app.py` from that repository.
5. In the app's **Settings → Secrets**, add:

   ```toml
   SUPABASE_URL = "https://YOUR_PROJECT_ID.supabase.co"
   SUPABASE_ANON_KEY = "YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY"
   ```

6. Set the Supabase Site URL and allowed redirect URL to the deployed `https://...streamlit.app`
   address. Users can then visit that public URL from anywhere and create an account. To
   enable passkeys, configure Supabase Auth's WebAuthn relying party ID and allowed origin
   for this exact HTTPS host.

The publishable/anon key is intended for client applications; access is constrained by
Supabase Auth, row-level security, and database functions. Never put a Supabase service-role
key in this app or in its secrets.

## User and workspace model

- A person creates an account using their own email and a password they choose for this app.
  This does not ask for or use their Gmail password.
- Email verification is performed by Supabase Auth.
- Users can sign in with a Supabase passkey or add/remove passkeys after signing in. The
  browser or password manager controls verification; it may use fingerprint, face, device
  PIN, or another supported authenticator. Passkeys require HTTPS and compatible
  browser/device configuration. Password sign-in remains available.
- A new account without an invite creates a private company workspace and becomes its admin;
  no existing admin needs to approve the signup.
- Admins generate one-time, seven-day invite codes for Finance, Contractor, or Viewer roles.
- Records are separated by workspace, and role-specific database functions validate every
  write on the server.
- Finance can view/update payments; a Contractor account sees and updates only its assigned
  contractor record; Viewer accounts are read-only and do not receive financial amounts.
- Project pictures can be captured with the device camera or uploaded. They are stored in a
  private Supabase Storage bucket with workspace/contractor access policies; uploads are
  limited to JPEG, PNG, or WebP images up to 10 MB.
- Contractor records store a phone number, and attendance records are saved per contractor
  and date in the database.
- PDFs and pictures can be saved as private contractor files. Configure optional AI providers
  by adding only the API keys you intend to use to Streamlit secrets (`OPENAI_API_KEY`,
  `GEMINI_API_KEY`, `XAI_API_KEY`). Messages are sent to the selected provider; don't send
  sensitive worker or contract information.
- The calculator runs locally in the app and does not send values to an AI provider.

## Local development

Install dependencies with `pip install -r requirements.txt`. Copy
`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`, enter the Supabase project
URL and publishable/anon key, run `supabase_schema.sql` in that project's SQL Editor, then
run `streamlit run app.py`. AI API keys are optional and may be added to the same secrets
file. The separate older local prototype stores data in a SQLite file beside the script
and attachments in `contractor_app_files`; it is intended for one computer, not shared
production hosting. Its database and attachments are not encrypted at rest, so protect
them with the computer's account permissions and backups. For multiple contractors or
remote access, deploy the hosted Supabase version instead. Passkey login is available on
the hosted site; the Android WebView wrapper uses the device's native biometric prompt
(with device screen-lock fallback where supported). The local desktop prototype continues
to use email and an app-specific password; it does not sign in through Google, and local
accounts are stored on that computer. On Android, enter the deployed site's HTTPS URL on
first launch; the wrapper blocks insecure HTTP pages. Devices without supported
biometrics continue to rely on the website's normal sign-in.
