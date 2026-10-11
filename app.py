from io import BytesIO
from datetime import date
from pathlib import Path
from uuid import uuid4

import httpx
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, UnidentifiedImageError
from postgrest.exceptions import APIError
from supabase import Client, create_client
from supabase_auth.errors import AuthError
from storage3.exceptions import StorageException


st.set_page_config(page_title="Contractor Portal", page_icon="🏗️", layout="wide")

passkey_component = components.declare_component(
    "contractor_passkeys", path=str(Path(__file__).parent / "passkey_component")
)


def required_secrets() -> tuple[str, str] | None:
    missing = [key for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY") if key not in st.secrets]
    if missing:
        st.error(
            "Hosting setup is incomplete. Add these secrets in the hosting dashboard: "
            + ", ".join(missing)
        )
        return None
    return st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"]


def make_client(url: str, anon_key: str) -> Client:
    return create_client(url, anon_key)


def store_session(session) -> None:
    st.session_state.auth_tokens = {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
    }


def clear_session(client: Client | None = None) -> None:
    st.session_state.pop("auth_tokens", None)
    st.session_state.pop("membership", None)
    if client is not None:
        client.auth.sign_out()


def handle_passkey_result(result, client: Client, scope: str) -> None:
    if not isinstance(result, dict):
        return
    event_id = result.get("event_id")
    if not event_id or event_id == st.session_state.get(f"last_passkey_event_{scope}"):
        return
    st.session_state[f"last_passkey_event_{scope}"] = event_id

    if result.get("status") == "error":
        st.error(f"Passkey action failed: {result.get('message', 'Unknown error')}")
        return

    if result.get("action") == "sign_in":
        session = result.get("session") or {}
        access_token = session.get("access_token")
        refresh_token = session.get("refresh_token")
        if not access_token or not refresh_token:
            st.error("Passkey sign-in did not return a valid session.")
            return
        try:
            client.auth.set_session(access_token, refresh_token)
            current_session = client.auth.get_session()
            if current_session is None:
                st.error("Passkey sign-in succeeded, but the session could not be loaded.")
                return
            store_session(current_session)
            st.rerun()
        except AuthError as error:
            st.error(f"Passkey session could not be started: {error}")
        return

    st.success("Passkey settings updated.")


def render_auth(client: Client, supabase_url: str, anon_key: str) -> None:
    st.title("🏗️ Contractor Portal")
    st.write("Securely manage your company's contractors, projects, and payments.")
    login_tab, signup_tab = st.tabs(["Sign in", "Create company account"])

    with login_tab:
        with st.form("login_form"):
            email = st.text_input("Email address")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", use_container_width=True)
        if submitted:
            try:
                response = client.auth.sign_in_with_password(
                    {"email": email.strip().lower(), "password": password}
                )
                if response.session is None:
                    st.error("Sign-in did not return a valid session. Please try again.")
                else:
                    store_session(response.session)
                    st.rerun()
            except AuthError:
                st.error("Sign-in failed. Check your email and password, then try again.")
        st.caption("Or use a passkey saved with your device or password manager.")
        result = passkey_component(
            mode="sign_in",
            supabase_url=supabase_url,
            anon_key=anon_key,
            key="contractor_passkey_sign_in",
            default=None,
            height=105,
        )
        handle_passkey_result(result, client, "sign_in")

    with signup_tab:
        st.info(
            "You can create your own account without approval from another admin. Verify "
            "your email to sign in. Your account starts a private company workspace; "
            "existing company teams can join with an invite code."
        )
        with st.form("signup_form"):
            name = st.text_input("Your name")
            company_name = st.text_input("Company name")
            email = st.text_input("Email address")
            password = st.text_input("Create password (at least 12 characters)", type="password")
            invite_code = st.text_input("Company team invite code (optional)", type="password")
            submitted = st.form_submit_button("Create account", use_container_width=True)
        if submitted:
            if not name.strip() or (not invite_code.strip() and not company_name.strip()):
                st.error("Your name and, for a new company, company name are required.")
            elif len(password) < 12:
                st.error("Choose a password with at least 12 characters.")
            else:
                try:
                    response = client.auth.sign_up(
                        {
                            "email": email.strip().lower(),
                            "password": password,
                            "options": {
                                "data": {
                                    "full_name": name.strip(),
                                    "company_name": company_name.strip(),
                                    "invite_code": invite_code.strip(),
                                }
                            },
                        }
                    )
                    if response.session is None:
                        st.success(
                            "Account created. Verify your email using the link we sent, "
                            "then sign in here. No company admin approval is required."
                        )
                    else:
                        store_session(response.session)
                        st.rerun()
                except AuthError as error:
                    st.error(f"Account creation failed: {error.message}")


def membership_for(client: Client) -> dict | None:
    response = client.rpc("get_my_membership").execute()
    if not response.data:
        return None
    return response.data[0]


def validate_image(image_bytes: bytes) -> tuple[str, str]:
    if not image_bytes or len(image_bytes) > 10 * 1024 * 1024:
        raise ValueError("Choose a picture smaller than 10 MB.")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Use a JPEG, PNG, or WebP image.")
            if image.width * image.height > 25_000_000:
                raise ValueError("Image dimensions are too large. Choose a smaller picture.")
            image.verify()
            image_format = image.format
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("That file is not a valid supported image.") from error
    extension, content_type = {
        "JPEG": ("jpg", "image/jpeg"),
        "PNG": ("png", "image/png"),
        "WEBP": ("webp", "image/webp"),
    }[image_format]
    return extension, content_type


def configured_ai_providers() -> dict[str, tuple[str, str, str]]:
    providers = {
        "ChatGPT": ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
        "Gemini": (
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "gemini-2.5-flash",
        ),
        "Grok": ("XAI_API_KEY", "https://api.x.ai/v1/chat/completions", "grok-3-mini"),
    }
    return {
        name: (st.secrets[key], endpoint, model)
        for name, (key, endpoint, model) in providers.items()
        if key in st.secrets and st.secrets[key]
    }


def render_ai_assistant() -> None:
    st.subheader("AI assistant")
    providers = configured_ai_providers()
    if not providers:
        st.info(
            "Add OPENAI_API_KEY, GEMINI_API_KEY, or XAI_API_KEY to app secrets to enable AI."
        )
        return
    st.caption(
        "Only messages you send in this chat are shared with the selected AI provider. "
        "Do not include private worker, payroll, or contract details."
    )
    provider_name = st.selectbox("AI provider", list(providers), key="ai_provider")
    if "ai_messages" not in st.session_state:
        st.session_state.ai_messages = []
    for message in st.session_state.ai_messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    prompt = st.chat_input("Ask about estimating, scheduling, safety, or project planning")
    if prompt:
        st.session_state.ai_messages.append({"role": "user", "content": prompt})
        api_key, endpoint, model = providers[provider_name]
        try:
            response = httpx.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a practical construction and contractor business "
                                "assistant. Give clear, safety-conscious guidance. Do not "
                                "claim to replace licensed professionals, site inspections, "
                                "legal advice, or engineering review."
                            ),
                        },
                        *st.session_state.ai_messages[-20:],
                    ],
                    "temperature": 0.3,
                },
                timeout=45,
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("The AI provider returned an empty response.")
            st.session_state.ai_messages.append(
                {"role": "assistant", "content": answer.strip()}
            )
            st.rerun()
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
            st.error(f"{provider_name} could not answer: {error}")


def render_calculator() -> None:
    st.subheader("Calculator")
    with st.form("calculator_form"):
        first = st.number_input("First number", value=0.0, key="calc_first")
        operation = st.selectbox("Operation", ["+", "−", "×", "÷"], key="calc_operation")
        second = st.number_input("Second number", value=0.0, key="calc_second")
        calculate = st.form_submit_button("Calculate")
    if calculate:
        if operation == "÷" and second == 0:
            st.error("Cannot divide by zero.")
            return
        result = {
            "+": lambda: first + second,
            "−": lambda: first - second,
            "×": lambda: first * second,
            "÷": lambda: first / second,
        }[operation]()
        st.success(f"Result: {result:,.4f}")


def render_project_photos(
    client: Client, membership: dict, contractors: pd.DataFrame
) -> None:
    st.subheader("📸 Photos and important files")
    if contractors.empty:
        st.info("Add or assign a contractor record before saving project photos.")
        return

    selected_id = st.selectbox(
        "Choose project / contractor",
        contractors["id"].tolist(),
        format_func=lambda value: contractors.loc[
            contractors["id"] == value, "contractor_name"
        ].iloc[0],
        key="photo_contractor",
    )
    selected = contractors.loc[contractors["id"] == selected_id].iloc[0]

    photo_tabs = st.tabs(["Take a picture", "Upload picture or file"])
    camera_photo = photo_tabs[0].camera_input(
        "Open camera and take a project picture",
        key=f"camera_photo_{selected_id}",
    )
    uploaded_photo = photo_tabs[1].file_uploader(
        "Choose an image, PDF, or text file (max 15 MB)",
        type=["jpg", "jpeg", "png", "webp", "pdf", "txt"],
        key=f"upload_photo_{selected_id}",
    )
    photo = camera_photo or uploaded_photo
    if photo is not None:
        photo_bytes = photo.getvalue()
        try:
            validate_image(photo_bytes)
            st.image(photo_bytes, caption=selected["contractor_name"], width=420)
        except ValueError as error:
            st.error(str(error))
            photo = None

    if photo is not None and st.button("Save project picture", key=f"save_photo_{selected_id}"):
        try:
            image_bytes = photo.getvalue()
            extension, content_type = validate_image(image_bytes)
            object_path = (
                f"{membership['workspace_id']}/{selected_id}/{uuid4().hex}.{extension}"
            )
            client.storage.from_("contractor-photos").upload(
                object_path,
                image_bytes,
                {"content-type": content_type, "upsert": "false"},
            )
            st.success("Picture saved privately to this company workspace.")
            st.rerun()
        except ValueError as error:
            st.error(str(error))
        except StorageException as error:
            st.error(f"Picture could not be saved: {error}")

    if uploaded_photo is not None and st.button(
        "Save important file", key=f"save_file_{selected_id}"
    ):
        file_bytes = uploaded_photo.getvalue()
        if not file_bytes or len(file_bytes) > 15 * 1024 * 1024:
            st.error("Choose a file smaller than 15 MB.")
        else:
            file_name = uploaded_photo.name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
            if extension not in {"jpg", "jpeg", "png", "webp", "pdf", "txt"}:
                st.error("Unsupported file type.")
            else:
                object_path = f"{membership['workspace_id']}/{selected_id}/{uuid4().hex}.{extension}"
                try:
                    client.storage.from_("contractor-files").upload(
                        object_path,
                        file_bytes,
                        {
                            "content-type": uploaded_photo.type,
                            "upsert": "false",
                        },
                    )
                    st.success("Important file saved privately to this contractor record.")
                    st.rerun()
                except StorageException as error:
                    st.error(f"File could not be saved: {error}")

    st.write("Saved pictures")
    try:
        storage = client.storage.from_("contractor-photos")
        folder = f"{membership['workspace_id']}/{selected_id}"
        saved_photos = storage.list(folder)
        saved_photos = sorted(saved_photos, key=lambda item: item.get("created_at", ""), reverse=True)
        if not saved_photos:
            st.caption("No pictures saved for this project yet.")
        else:
            columns = st.columns(3)
            for index, saved_photo in enumerate(saved_photos):
                if not saved_photo.get("name"):
                    continue
                path = f"{folder}/{saved_photo['name']}"
                signed_url = storage.create_signed_url(path, 3600).get("signedURL")
                if signed_url:
                    with columns[index % len(columns)]:
                        st.image(signed_url, caption=saved_photo["name"], use_container_width=True)
    except StorageException as error:
        st.error(f"Saved pictures could not be loaded: {error}")

    st.write("Saved important files")
    try:
        storage = client.storage.from_("contractor-files")
        folder = f"{membership['workspace_id']}/{selected_id}"
        saved_files = sorted(
            storage.list(folder),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )
        if not saved_files:
            st.caption("No documents saved for this contractor yet.")
        for saved_file in saved_files:
            file_name = saved_file.get("name")
            if file_name:
                file_path = f"{folder}/{file_name}"
                signed_url = storage.create_signed_url(file_path, 3600).get("signedURL")
                if signed_url:
                    st.link_button(f"Open {file_name}", signed_url)
    except StorageException as error:
        st.error(f"Saved files could not be loaded: {error}")


def fetch_contractors(client: Client) -> pd.DataFrame:
    response = client.rpc("get_contractors").execute()
    return pd.DataFrame(response.data)


def render_attendance(client: Client, role: str, contractors: pd.DataFrame) -> None:
    st.subheader("🗓️ Contractor attendance")
    selected_date = st.date_input("Attendance date", value=date.today(), key="attendance_date")
    if role in {"Admin", "Contractor"} and not contractors.empty:
        selected_id = st.selectbox(
            "Contractor",
            contractors["id"].tolist(),
            format_func=lambda value: contractors.loc[
                contractors["id"] == value, "contractor_name"
            ].iloc[0],
            key="attendance_contractor",
        )
        with st.form("attendance_form"):
            attendance_status = st.selectbox("Attendance", ["Present", "Absent", "Leave"])
            notes = st.text_input("Notes (optional)")
            submitted = st.form_submit_button("Save attendance")
        if submitted:
            try:
                client.rpc(
                    "mark_attendance",
                    {
                        "target_contractor_id": selected_id,
                        "work_date": str(selected_date),
                        "new_status": attendance_status,
                        "new_notes": notes.strip(),
                    },
                ).execute()
                st.success("Attendance saved.")
                st.rerun()
            except APIError as error:
                st.error(f"Attendance could not be saved: {error.message}")
    elif role in {"Admin", "Contractor"}:
        st.info("Add or assign a contractor record before marking attendance.")

    try:
        rows = client.rpc(
            "get_attendance",
            {
                "start_date": str(selected_date.replace(day=1)),
                "end_date": str(selected_date),
            },
        ).execute()
        st.dataframe(pd.DataFrame(rows.data), use_container_width=True, hide_index=True)
    except APIError as error:
        st.error(f"Attendance could not be loaded: {error.message}")


def render_admin(client: Client, contractors: pd.DataFrame) -> None:
    directory, onboard, invites = st.tabs(
        ["Contractor directory", "Add contractor", "Team access"]
    )

    with directory:
        status_filter = st.selectbox(
            "Filter by status", ["All", "Pending", "Active", "Completed"]
        )
        filtered = contractors
        if status_filter != "All" and not contractors.empty:
            filtered = contractors[contractors["status"] == status_filter]
        st.dataframe(filtered, use_container_width=True, hide_index=True)

        if not contractors.empty:
            contractor_id = st.selectbox(
                "Select record to edit",
                contractors["id"].tolist(),
                format_func=lambda value: contractors.loc[
                    contractors["id"] == value, "contractor_name"
                ].iloc[0],
            )
            selected = contractors.loc[contractors["id"] == contractor_id].iloc[0]
            with st.form("edit_contractor"):
                name = st.text_input("Contractor name", value=selected["contractor_name"])
                phone = st.text_input("Phone number", value=selected["phone_number"] or "")
                specialty = st.text_input("Specialty", value=selected["specialty"] or "")
                project = st.text_input(
                    "Project assignment", value=selected["project_assignment"] or ""
                )
                value = st.number_input(
                    "Contract value",
                    min_value=0.0,
                    value=float(selected["contract_value"]),
                    step=500.0,
                )
                paid = st.number_input(
                    "Amount paid",
                    min_value=0.0,
                    value=float(selected["amount_paid"]),
                    step=500.0,
                )
                start_date = st.date_input("Start date", value=date.fromisoformat(selected["start_date"]))
                end_date = st.date_input("End date", value=date.fromisoformat(selected["end_date"]))
                status = st.selectbox(
                    "Status",
                    ["Pending", "Active", "Completed"],
                    index=["Pending", "Active", "Completed"].index(selected["status"]),
                )
                submitted = st.form_submit_button("Save changes")
            if submitted:
                if not name.strip() or not specialty.strip():
                    st.error("Contractor name and specialty are required.")
                elif paid > value:
                    st.error("Amount paid cannot exceed the contract value.")
                else:
                    client.rpc(
                        "edit_contractor",
                        {
                            "target_id": contractor_id,
                            "new_name": name.strip(),
                            "new_phone": phone.strip(),
                            "new_specialty": specialty.strip(),
                            "new_project": project.strip(),
                            "new_value": value,
                            "new_paid": paid,
                            "new_start_date": str(start_date),
                            "new_end_date": str(end_date),
                            "new_status": status,
                        },
                    ).execute()
                    st.success("Contractor record updated.")
                    st.rerun()

    with onboard:
        with st.form("add_contractor"):
            name = st.text_input("Contractor name")
            phone = st.text_input("Phone number")
            specialty = st.text_input("Specialty")
            project = st.text_input("Project assignment")
            value = st.number_input("Contract value", min_value=0.0, step=500.0)
            paid = st.number_input("Initial amount paid", min_value=0.0, step=500.0)
            start_date = st.date_input("Start date")
            end_date = st.date_input("End date")
            status = st.selectbox("Status", ["Pending", "Active", "Completed"])
            submitted = st.form_submit_button("Add contractor")
        if submitted:
            if not name.strip() or not specialty.strip():
                st.error("Contractor name and specialty are required.")
            elif paid > value:
                st.error("Amount paid cannot exceed the contract value.")
            else:
                client.rpc(
                    "create_contractor",
                    {
                        "new_name": name.strip(),
                        "new_phone": phone.strip(),
                        "new_specialty": specialty.strip(),
                        "new_project": project.strip(),
                        "new_value": value,
                        "new_paid": paid,
                        "new_start_date": str(start_date),
                        "new_end_date": str(end_date),
                        "new_status": status,
                    },
                ).execute()
                st.success("Contractor added.")
                st.rerun()

    with invites:
        st.write("Invite a teammate to your private company workspace.")
        role = st.selectbox("Teammate role", ["Finance", "Contractor", "Viewer"])
        email = st.text_input("Teammate email address")
        contractor_id = None
        if role == "Contractor":
            if contractors.empty:
                st.info("Add a contractor record before inviting a contractor user.")
            else:
                contractor_id = st.selectbox(
                    "Assign this account to",
                    contractors["id"].tolist(),
                    format_func=lambda value: contractors.loc[
                        contractors["id"] == value, "contractor_name"
                    ].iloc[0],
                )
        if st.button("Generate one-time invite code"):
            if not email.strip():
                st.error("Enter the teammate's email address.")
            elif role == "Contractor" and contractor_id is None:
                st.error("Choose the contractor record for this account.")
            else:
                response = client.rpc(
                    "create_workspace_invite",
                    {
                        "requested_role": role,
                        "invited_email": email.strip().lower(),
                        "target_contractor_id": contractor_id,
                    },
                ).execute()
                st.code(response.data)
                st.caption(
                    "Share this code privately with that email address. "
                    "It can be used once and expires in 7 days."
                )


def render_role_dashboard(client: Client, role: str, contractors: pd.DataFrame) -> None:
    if role == "Admin":
        render_admin(client, contractors)
        return

    if role == "Finance":
        finance_df = contractors.copy()
        if not finance_df.empty:
            finance_df["balance_due"] = (
                finance_df["contract_value"] - finance_df["amount_paid"]
            )
        st.subheader("Financial overview")
        st.dataframe(finance_df, use_container_width=True, hide_index=True)
        if contractors.empty:
            return
        contractor_id = st.selectbox(
            "Contractor",
            contractors["id"].tolist(),
            format_func=lambda value: contractors.loc[
                contractors["id"] == value, "contractor_name"
            ].iloc[0],
        )
        selected = contractors.loc[contractors["id"] == contractor_id].iloc[0]
        with st.form("update_payment"):
            amount = st.number_input(
                "Total amount paid",
                min_value=0.0,
                max_value=float(selected["contract_value"]),
                value=float(selected["amount_paid"]),
                step=500.0,
            )
            submitted = st.form_submit_button("Save payment")
        if submitted:
            client.rpc(
                "update_contractor_payment",
                {"target_id": contractor_id, "new_paid": amount},
            ).execute()
            st.success("Payment updated.")
            st.rerun()
        return

    if role == "Contractor":
        st.subheader("Contractor work panel")
        if contractors.empty:
            st.info("No contractor records are assigned to this account.")
            return
        visible_columns = [
            "contractor_name",
            "specialty",
            "project_assignment",
            "start_date",
            "end_date",
            "status",
        ]
        st.dataframe(contractors[visible_columns], use_container_width=True, hide_index=True)
        selected = contractors.iloc[0]
        with st.form("update_status"):
            status = st.selectbox(
                "Project status",
                ["Pending", "Active", "Completed"],
                index=["Pending", "Active", "Completed"].index(selected["status"]),
            )
            submitted = st.form_submit_button("Save status")
        if submitted:
            client.rpc(
                "update_contractor_status",
                {"target_id": selected["id"], "new_status": status},
            ).execute()
            st.success("Project status updated.")
            st.rerun()
        return

    st.subheader("Read-only overview")
    st.dataframe(contractors, use_container_width=True, hide_index=True)


credentials = required_secrets()
if credentials is None:
    st.stop()

supabase = st.session_state.get("supabase_client")
if supabase is None:
    supabase = make_client(*credentials)
    st.session_state.supabase_client = supabase
    if "auth_tokens" in st.session_state:
        try:
            supabase.auth.set_session(
                st.session_state.auth_tokens["access_token"],
                st.session_state.auth_tokens["refresh_token"],
            )
        except AuthError:
            clear_session()
            st.warning("Your session expired. Please sign in again.")

if "auth_tokens" not in st.session_state:
    render_auth(supabase, *credentials)
    st.stop()

try:
    current_session = supabase.auth.get_session()
    if current_session is None:
        clear_session()
        st.rerun()
    store_session(current_session)
except AuthError:
    clear_session()
    st.warning("Your session expired. Please sign in again.")
    st.rerun()

try:
    membership = membership_for(supabase)
except APIError as error:
    st.error(f"Could not load your company membership: {error.message}")
    st.stop()

if membership is None:
    st.error(
        "Your account has no company membership. Ask your company admin to invite you again."
    )
    st.stop()

st.title(f"🏗️ {membership['workspace_name']}")
st.caption(f"Signed in as {membership['email']} · {membership['role']}")

with st.sidebar:
    st.subheader("Account")
    st.write(membership["email"])
    st.write(f"Role: **{membership['role']}**")
    with st.expander("Passkey security"):
        st.caption(
            "Register a device passkey, or remove one you no longer use. "
            "Your device decides whether to verify with biometrics, a PIN, or another "
            "supported authenticator."
        )
        passkey_result = passkey_component(
            mode="manage",
            supabase_url=credentials[0],
            anon_key=credentials[1],
            access_token=st.session_state.auth_tokens["access_token"],
            refresh_token=st.session_state.auth_tokens["refresh_token"],
            key="contractor_passkey_manage",
            default=None,
            height=330,
        )
        handle_passkey_result(passkey_result, supabase, "manage")
    if st.button("Sign out"):
        clear_session(supabase)
        st.rerun()

try:
    contractors = fetch_contractors(supabase)
except APIError as error:
    st.error(f"Could not load contractor records: {error.message}")
    st.stop()

if contractors.empty:
    total_value = total_paid = 0.0
else:
    total_value = float(contractors["contract_value"].sum())
    total_paid = float(contractors["amount_paid"].sum())

metric_columns = st.columns(2 if membership["role"] not in ("Admin", "Finance") else 4)
metric_columns[0].metric("Contractors", len(contractors))
metric_columns[1].metric(
    "Active projects",
    int((contractors["status"] == "Active").sum()) if not contractors.empty else 0,
)
if membership["role"] in ("Admin", "Finance"):
    metric_columns[2].metric("Contract value", f"${total_value:,.2f}")
    metric_columns[3].metric("Balance due", f"${total_value - total_paid:,.2f}")

try:
    render_role_dashboard(supabase, membership["role"], contractors)
except APIError as error:
    st.error(f"That action could not be completed: {error.message}")

render_attendance(supabase, membership["role"], contractors)
with st.expander("🧮 In-app calculator"):
    render_calculator()
render_project_photos(supabase, membership, contractors)
render_ai_assistant()
