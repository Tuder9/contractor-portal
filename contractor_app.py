import hashlib
import httpx
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st
from PIL import Image, UnidentifiedImageError

st.set_page_config(page_title="Contractor Record Ledger", page_icon="🏗️", layout="wide")

st.markdown(
    """
    <style>
    .stApp { background: #f3f6fb; }
    .stSidebar > div { background: #ffffff; }
    .metric-box { background-color: #ffffff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .stButton>button { background-color: #2e86de; color: white; border-radius: 6px; }
    </style>
    """,
    unsafe_allow_html=True,
)

DATABASE_PATH = Path(__file__).with_name("contractor_app_data.sqlite3")
FILES_DIRECTORY = Path(__file__).with_name("contractor_app_files")

if "registered_users" not in st.session_state:
    st.session_state.registered_users = {}

if "contractors" not in st.session_state:
    st.session_state.contractors = [
        {
            "ID": "CON-001",
            "Name": "Alpha Electric Corp",
            "Specialty": "Electrical Systems",
            "Project Assignment": "Downtown Commercial Hub",
            "Contract Value ($)": 45000.00,
            "Amount Paid ($)": 15000.00,
            "Start Date": "2026-01-15",
            "End Date": "2026-08-30",
            "Status": "Active",
        },
        {
            "ID": "CON-002",
            "Name": "BuildWright Masonry",
            "Specialty": "Structural Concrete",
            "Project Assignment": "Westside Parking Structure",
            "Contract Value ($)": 82000.00,
            "Amount Paid ($)": 82000.00,
            "Start Date": "2025-09-01",
            "End Date": "2026-03-15",
            "Status": "Completed",
        },
        {
            "ID": "CON-003",
            "Name": "Apex HVAC Solutions",
            "Specialty": "Climate Systems",
            "Project Assignment": "Downtown Commercial Hub",
            "Contract Value ($)": 38000.00,
            "Amount Paid ($)": 0.00,
            "Start Date": "2026-07-01",
            "End Date": "2026-12-15",
            "Status": "Pending",
        },
    ]

if "user" not in st.session_state:
    st.session_state.user = None


def hash_password(password):
    salt = secrets.token_hex(16).encode("utf-8")
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + derived.hex()


def verify_password(password, stored_hash):
    try:
        salt_hex, digest_hex = stored_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000).hex()
        return secrets.compare_digest(expected, digest_hex)
    except (ValueError, TypeError):
        return False


def is_valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


def get_df():
    return pd.DataFrame(st.session_state.contractors)


@contextmanager
def open_local_database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_contractors():
    with open_local_database() as connection:
        connection.executemany(
            """
            INSERT INTO contractors (
                id, name, phone, specialty, project, contract_value, amount_paid,
                start_date, end_date, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, phone=excluded.phone,
                specialty=excluded.specialty, project=excluded.project,
                contract_value=excluded.contract_value, amount_paid=excluded.amount_paid,
                start_date=excluded.start_date, end_date=excluded.end_date,
                status=excluded.status
            """,
            [
                (
                    item["ID"], item["Name"], item.get("Phone number", ""),
                    item["Specialty"], item["Project Assignment"],
                    item["Contract Value ($)"], item["Amount Paid ($)"],
                    item["Start Date"], item["End Date"], item["Status"],
                )
                for item in st.session_state.contractors
            ],
        )


def initialize_local_storage():
    with open_local_database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS contractors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                specialty TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                contract_value REAL NOT NULL,
                amount_paid REAL NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY,
                contractor_id TEXT NOT NULL REFERENCES contractors(id),
                attendance_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('Present', 'Absent', 'Leave')),
                notes TEXT NOT NULL DEFAULT '',
                UNIQUE(contractor_id, attendance_date)
            );
            """
        )
        contractor_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(contractors)")
        }
        if "phone" not in contractor_columns:
            connection.execute(
                "ALTER TABLE contractors ADD COLUMN phone TEXT NOT NULL DEFAULT ''"
            )
        count = connection.execute("SELECT COUNT(*) FROM contractors").fetchone()[0]
        if count == 0:
            connection.executemany(
                """
                INSERT INTO contractors (
                    id, name, specialty, project, contract_value, amount_paid,
                    start_date, end_date, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["ID"], item["Name"], item["Specialty"],
                        item["Project Assignment"], item["Contract Value ($)"],
                        item["Amount Paid ($)"], item["Start Date"],
                        item["End Date"], item["Status"],
                    )
                    for item in st.session_state.contractors
                ],
            )
        rows = connection.execute("SELECT * FROM contractors ORDER BY id").fetchall()
        st.session_state.contractors = [
            {
                "ID": row["id"],
                "Name": row["name"],
                "Phone number": row["phone"],
                "Specialty": row["specialty"],
                "Project Assignment": row["project"],
                "Contract Value ($)": row["contract_value"],
                "Amount Paid ($)": row["amount_paid"],
                "Start Date": row["start_date"],
                "End Date": row["end_date"],
                "Status": row["status"],
            }
            for row in rows
        ]
        user_rows = connection.execute("SELECT * FROM users").fetchall()
        st.session_state.registered_users = {
            row["email"]: {
                "role": row["role"],
                "name": row["name"],
                "password_hash": row["password_hash"],
            }
            for row in user_rows
        }


def save_attendance(contractor_id, work_date, status, notes):
    with open_local_database() as connection:
        connection.execute(
            """
            INSERT INTO attendance (contractor_id, attendance_date, status, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(contractor_id, attendance_date)
            DO UPDATE SET status=excluded.status, notes=excluded.notes
            """,
            (contractor_id, str(work_date), status, notes.strip()),
        )


def save_local_attachment(contractor_id, uploaded_file):
    suffix = Path(uploaded_file.name).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".txt"}:
        raise ValueError("Only JPEG, PNG, WebP, PDF, and text files are supported.")
    file_bytes = uploaded_file.getvalue()
    if not file_bytes or len(file_bytes) > 15 * 1024 * 1024:
        raise ValueError("Choose a file smaller than 15 MB.")
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        try:
            with Image.open(uploaded_file) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Use a JPEG, PNG, or WebP picture.")
                if image.width * image.height > 25_000_000:
                    raise ValueError(
                        "Picture dimensions are too large. Choose a smaller image."
                    )
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValueError("That picture is not a valid image.") from error
    contractor_directory = FILES_DIRECTORY / re.sub(r"[^A-Za-z0-9_-]", "_", contractor_id)
    contractor_directory.mkdir(parents=True, exist_ok=True)
    saved_path = contractor_directory / f"{uuid4().hex}{suffix}"
    saved_path.write_bytes(file_bytes)
    return saved_path


def render_local_tools():
    st.subheader("🧮 In-app calculator")
    with st.form("local_calculator"):
        left = st.number_input("First number", value=0.0)
        operation = st.selectbox("Operation", ["+", "−", "×", "÷"])
        right = st.number_input("Second number", value=0.0)
        calculate = st.form_submit_button("Calculate")
    if calculate:
        if operation == "÷" and right == 0:
            st.error("Cannot divide by zero.")
        else:
            result = {
                "+": lambda: left + right,
                "−": lambda: left - right,
                "×": lambda: left * right,
                "÷": lambda: left / right,
            }[operation]()
            st.success(f"Result: {result:,.4f}")

    st.subheader("🤖 AI assistant")
    providers = {
        "ChatGPT": ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
        "Gemini": (
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "gemini-2.5-flash",
        ),
        "Grok": ("XAI_API_KEY", "https://api.x.ai/v1/chat/completions", "grok-3-mini"),
    }
    configured = {}
    try:
        configured = {
            name: (st.secrets[key], endpoint, model)
            for name, (key, endpoint, model) in providers.items()
            if key in st.secrets and st.secrets[key]
        }
    except FileNotFoundError:
        pass
    if not configured:
        st.info("Configure an AI provider API key in Streamlit secrets to enable chat.")
        return
    st.caption("Chat messages are sent to the selected provider. Avoid sharing private records.")
    provider_name = st.selectbox("AI provider", list(configured), key="local_ai_provider")
    if "local_ai_messages" not in st.session_state:
        st.session_state.local_ai_messages = []
    for message in st.session_state.local_ai_messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    prompt = st.chat_input("Ask a contractor business question")
    if prompt:
        st.session_state.local_ai_messages.append({"role": "user", "content": prompt})
        api_key, endpoint, model = configured[provider_name]
        try:
            response = httpx.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Give practical, safety-conscious contractor business guidance.",
                        },
                        *st.session_state.local_ai_messages[-20:],
                    ],
                },
                timeout=45,
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]
            st.session_state.local_ai_messages.append(
                {"role": "assistant", "content": answer}
            )
            st.rerun()
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
            st.error(f"{provider_name} could not answer: {error}")


def render_local_attendance_and_files():
    attendance_tab, files_tab = st.tabs(["Attendance", "Photos and important files"])
    contractor_ids = [item["ID"] for item in st.session_state.contractors]

    with attendance_tab:
        st.subheader("Saved contractor attendance")
        if current_role == "Admin" and contractor_ids:
            selected_id = st.selectbox(
                "Contractor",
                contractor_ids,
                format_func=lambda contractor_id: next(
                    item["Name"]
                    for item in st.session_state.contractors
                    if item["ID"] == contractor_id
                ),
            )
            with st.form("local_attendance_form"):
                work_date = st.date_input("Attendance date", value=datetime.today().date())
                attendance_status = st.selectbox(
                    "Attendance", ["Present", "Absent", "Leave"]
                )
                notes = st.text_input("Notes (optional)")
                submitted = st.form_submit_button("Save attendance")
            if submitted:
                save_attendance(selected_id, work_date, attendance_status, notes)
                st.success("Attendance saved on this computer.")
                st.rerun()
        elif current_role != "Admin":
            st.info("Ask a local app administrator to record attendance.")

        with open_local_database() as connection:
            attendance_rows = connection.execute(
                """
                SELECT a.attendance_date, c.name AS contractor, c.phone AS phone_number,
                       a.status, a.notes
                FROM attendance a
                JOIN contractors c ON c.id = a.contractor_id
                ORDER BY a.attendance_date DESC, c.name
                """
            ).fetchall()
        st.dataframe(
            pd.DataFrame([dict(row) for row in attendance_rows]),
            use_container_width=True,
            hide_index=True,
        )

    with files_tab:
        if not contractor_ids:
            st.info("Add a contractor record before attaching files.")
            return
        selected_id = st.selectbox(
            "Attach to contractor",
            contractor_ids,
            format_func=lambda contractor_id: next(
                item["Name"]
                for item in st.session_state.contractors
                if item["ID"] == contractor_id
            ),
            key="local_file_contractor",
        )
        camera_image = st.camera_input("Take a project picture", key="local_camera")
        uploaded_file = st.file_uploader(
            "Upload a photo or important file",
            type=["jpg", "jpeg", "png", "webp", "pdf", "txt"],
            key="local_file_upload",
        )
        selected_file = camera_image or uploaded_file
        if selected_file is not None and st.button("Save file on this computer"):
            try:
                saved_path = save_local_attachment(selected_id, selected_file)
                st.success(f"Saved as {saved_path.name}.")
                st.rerun()
            except (OSError, ValueError) as error:
                st.error(f"File could not be saved: {error}")

        contractor_directory = FILES_DIRECTORY / re.sub(r"[^A-Za-z0-9_-]", "_", selected_id)
        if contractor_directory.exists():
            for saved_path in sorted(contractor_directory.iterdir()):
                if not saved_path.is_file():
                    continue
                if saved_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    st.image(str(saved_path), caption=saved_path.name, width=360)
                st.download_button(
                    f"Download {saved_path.name}",
                    data=saved_path.read_bytes(),
                    file_name=saved_path.name,
                    key=f"download_{selected_id}_{saved_path.name}",
                )


def style_status(val):
    if val == "Active":
        return "background-color: #dff9fb; font-weight: bold; color: black;"
    elif val == "Completed":
        return "background-color: #badc58; font-weight: bold; color: black;"
    return "background-color: #f6e58d; font-weight: bold; color: black;"


initialize_local_storage()


with st.sidebar:
    st.title("🔐 Secure Contractor Login")
    if st.session_state.user is None:
        with st.form("login_form"):
            email = st.text_input("Email address")
            password = st.text_input("App password", type="password")
            login_btn = st.form_submit_button("Login")

        if login_btn:
            normalized_email = email.strip().lower()
            user_record = st.session_state.registered_users.get(normalized_email)

            if user_record and verify_password(password, user_record["password_hash"]):
                st.session_state.user = {
                    "role": user_record["role"],
                    "email": normalized_email,
                    "name": user_record["name"],
                }
                st.rerun()
            else:
                st.error("Invalid email address or app password.")

        st.write("---")
        st.subheader("Create an Account")
        st.caption(
            "Use your email address and a password created for this app. This is not "
            "Google sign-in; local accounts are saved on this computer."
        )
        with st.form("register_form"):
            new_name = st.text_input("Full Name")
            new_email = st.text_input("Email address")
            new_password = st.text_input("Create app password", type="password")
            confirm_password = st.text_input("Confirm Password", type="password")
            register_btn = st.form_submit_button("Sign Up")

        if register_btn:
            if not new_name.strip():
                st.error("Full name is required.")
            elif not is_valid_email(new_email):
                st.error("Please enter a valid email address.")
            elif len(new_password) < 8:
                st.error("Password must be at least 8 characters long.")
            elif new_password != confirm_password:
                st.error("Passwords do not match.")
            elif new_email.lower() in st.session_state.registered_users:
                st.error("This email address is already registered.")
            else:
                normalized_email = new_email.strip().lower()
                chosen_role = "Admin" if not st.session_state.registered_users else "Contractor"
                password_hash = hash_password(new_password)
                st.session_state.registered_users[normalized_email] = {
                    "role": chosen_role,
                    "name": new_name.strip(),
                    "password_hash": password_hash,
                }
                with open_local_database() as connection:
                    connection.execute(
                        "INSERT INTO users (email, name, role, password_hash) VALUES (?, ?, ?, ?)",
                        (normalized_email, new_name.strip(), chosen_role, password_hash),
                    )
                st.success(
                    f"Account created for {normalized_email} as {chosen_role}. "
                    "Use your email and app password to sign in."
                )
                st.rerun()
    else:
        st.success(f"Logged in as {st.session_state.user['role']} ({st.session_state.user['email']})")
        if st.button("Logout"):
            st.session_state.user = None
            st.rerun()

if st.session_state.user is None:
    st.title("🏗️ Contractor Record & Management Portal")
    st.info("Please log in using the sidebar to access the contractor system.")
    st.stop()

current_user = st.session_state.user
current_role = current_user["role"]

df = get_df()

st.title(f"🏗️ Contractor Portal · {current_role}")
st.write("Track profiles, project assignments, timeline schedules, and financial balances in one centralized ledger.")
st.write("---")

metrics = {
    "Total Contractors": len(df),
    "Active Work Sites": len(df[df["Status"] == "Active"]),
    "Total Contract Budget": f"${df['Contract Value ($)'].sum():,.2f}",
    "Remaining Balance Owed": f"${(df['Contract Value ($)'].sum() - df['Amount Paid ($)'].sum()):,.2f}",
}

col1, col2, col3, col4 = st.columns(4)
for col, (label, value) in zip((col1, col2, col3, col4), metrics.items()):
    with col:
        st.metric(label=label, value=value)

st.write("---")


if current_role == "Admin":
    left_panel, right_panel = st.columns([2, 1])

    with left_panel:
        st.subheader("📋 Contractor Directory Registry")
        status_filter = st.selectbox("Filter Directory View by Status:", ["All Records", "Active", "Pending", "Completed"])
        filtered_df = df if status_filter == "All Records" else df[df["Status"] == status_filter]
        st.dataframe(filtered_df.style.map(style_status, subset=["Status"]), use_container_width=True, hide_index=True)

    with right_panel:
        st.subheader("➕ Onboard New Contractor Record")
        with st.form("contractor_onboarding_form", clear_on_submit=True):
            new_name = st.text_input("Contractor/Business Entity Name*")
            new_phone = st.text_input("Phone number")
            new_specialty = st.text_input("Trade Specialty")
            new_project = st.text_input("Assigned Worksite Project / Location")

            c1, c2 = st.columns(2)
            with c1:
                new_value = st.number_input("Total Contract Value ($)", min_value=0.0, step=500.0)
            with c2:
                new_paid = st.number_input("Initial Upfront Payment Issued ($)", min_value=0.0, step=500.0)

            d1, d2 = st.columns(2)
            with d1:
                start_d = st.date_input("Contract Start Date", datetime.today())
            with d2:
                end_d = st.date_input("Contract Target End Date", datetime.today())

            new_status = st.selectbox("Current Operational Status", ["Pending", "Active", "Completed"])
            submit_btn = st.form_submit_button("Commit Entry to Database Ledger")

            if submit_btn:
                if not new_name.strip() or not new_specialty.strip():
                    st.error("Business Name and Trade Specialty fields cannot be blank.")
                elif new_paid > new_value:
                    st.error("Amount paid cannot exceed total contract value.")
                else:
                    next_index_id = f"CON-{str(len(st.session_state.contractors) + 1).zfill(3)}"
                    new_entry = {
                        "ID": next_index_id,
                        "Name": new_name,
                        "Phone number": new_phone.strip(),
                        "Specialty": new_specialty,
                        "Project Assignment": new_project,
                        "Contract Value ($)": float(new_value),
                        "Amount Paid ($)": float(new_paid),
                        "Start Date": str(start_d),
                        "End Date": str(end_d),
                        "Status": new_status,
                    }
                    st.session_state.contractors.append(new_entry)
                    save_contractors()
                    st.success(f"Added '{new_name}' with ID {next_index_id}.")
                    st.rerun()

        st.write("---")
        st.subheader("✏️ Edit Existing Contractor Record")
        contractor_ids = [contractor["ID"] for contractor in st.session_state.contractors]
        selected_edit_id = st.selectbox("Choose contractor to update", contractor_ids)
        selected_record = next((contractor for contractor in st.session_state.contractors if contractor["ID"] == selected_edit_id), None)

        if selected_record:
            with st.form("contractor_edit_form", clear_on_submit=False):
                edit_name = st.text_input("Contractor/Business Entity Name*", value=selected_record["Name"])
                edit_phone = st.text_input(
                    "Phone number", value=selected_record.get("Phone number", "")
                )
                edit_specialty = st.text_input("Trade Specialty", value=selected_record["Specialty"])
                edit_project = st.text_input("Assigned Worksite Project / Location", value=selected_record["Project Assignment"])

                e1, e2 = st.columns(2)
                with e1:
                    edit_value = st.number_input("Total Contract Value ($)", min_value=0.0, step=500.0, value=float(selected_record["Contract Value ($)"]))
                with e2:
                    edit_paid = st.number_input("Initial Upfront Payment Issued ($)", min_value=0.0, step=500.0, value=float(selected_record["Amount Paid ($)"]))

                d1, d2 = st.columns(2)
                with d1:
                    edit_start = st.date_input("Contract Start Date", value=datetime.strptime(selected_record["Start Date"], "%Y-%m-%d").date())
                with d2:
                    edit_end = st.date_input("Contract Target End Date", value=datetime.strptime(selected_record["End Date"], "%Y-%m-%d").date())

                edit_status = st.selectbox("Current Operational Status", ["Pending", "Active", "Completed"], index=["Pending", "Active", "Completed"].index(selected_record["Status"]))

                if st.form_submit_button("Save Contractor Changes"):
                    if not edit_name.strip() or not edit_specialty.strip():
                        st.error("Business Name and Trade Specialty fields cannot be blank.")
                    elif edit_paid > edit_value:
                        st.error("Amount paid cannot exceed total contract value.")
                    else:
                        selected_record["Name"] = edit_name
                        selected_record["Phone number"] = edit_phone.strip()
                        selected_record["Specialty"] = edit_specialty
                        selected_record["Project Assignment"] = edit_project
                        selected_record["Contract Value ($)"] = float(edit_value)
                        selected_record["Amount Paid ($)"] = float(edit_paid)
                        selected_record["Start Date"] = str(edit_start)
                        selected_record["End Date"] = str(edit_end)
                        selected_record["Status"] = edit_status
                        save_contractors()
                        st.success(f"Updated record for '{edit_name}'.")
                        st.rerun()

elif current_role == "Finance":
    st.subheader("💰 Financial Overview")
    finance_df = df[["ID", "Name", "Project Assignment", "Contract Value ($)", "Amount Paid ($)", "Status"]].copy()
    finance_df["Balance Due"] = finance_df["Contract Value ($)"] - finance_df["Amount Paid ($)"]
    st.dataframe(finance_df, use_container_width=True, hide_index=True)

    st.write("---")
    st.subheader("💳 Update Payment Amount")
    finance_record_id = st.selectbox("Choose contractor to update payment", [contractor["ID"] for contractor in st.session_state.contractors])
    finance_record = next((contractor for contractor in st.session_state.contractors if contractor["ID"] == finance_record_id), None)

    if finance_record:
        with st.form("finance_payment_form"):
            st.write(f"Selected: {finance_record['Name']} - {finance_record['Project Assignment']}")
            new_paid = st.number_input("Amount Paid ($)", min_value=0.0, value=float(finance_record["Amount Paid ($)"]), step=500.0)
            if st.form_submit_button("Save Payment Update"):
                finance_record["Amount Paid ($)"] = float(new_paid)
                if finance_record["Amount Paid ($)"] >= finance_record["Contract Value ($)"]:
                    finance_record["Status"] = "Completed"
                elif finance_record["Amount Paid ($)"] > 0:
                    finance_record["Status"] = "Active"
                save_contractors()
                st.success(f"Updated payment for {finance_record['Name']}.")
                st.rerun()

elif current_role == "Contractor":
    st.subheader("🧰 Contractor Work Panel")
    contractor_df = df[
        [
            "ID",
            "Name",
            "Phone number",
            "Project Assignment",
            "Specialty",
            "Status",
            "Start Date",
            "End Date",
        ]
    ]
    st.dataframe(contractor_df, use_container_width=True, hide_index=True)

    st.write("---")
    st.subheader("🛠️ Update My Status")
    contractor_id = st.selectbox("Choose contractor entry", [contractor["ID"] for contractor in st.session_state.contractors])
    contractor_record = next((contractor for contractor in st.session_state.contractors if contractor["ID"] == contractor_id), None)

    if contractor_record:
        with st.form("contractor_status_form"):
            st.write(f"Project: {contractor_record['Project Assignment']}")
            contractor_status = st.selectbox("Project Status", ["Pending", "Active", "Completed"], index=["Pending", "Active", "Completed"].index(contractor_record["Status"]))
            if st.form_submit_button("Save Status"):
                contractor_record["Status"] = contractor_status
                save_contractors()
                st.success(f"Status updated for {contractor_record['Name']}.")
                st.rerun()

else:
    st.subheader("👀 Read-Only Overview")
    read_only_df = df[["ID", "Name", "Specialty", "Project Assignment", "Status", "Contract Value ($)", "Amount Paid ($)"]]
    st.dataframe(read_only_df.style.map(style_status, subset=["Status"]), use_container_width=True, hide_index=True)
    st.write("---")
    status_filter = st.selectbox("Filter by status", ["All Records", "Active", "Pending", "Completed"])
    if status_filter != "All Records":
        read_only_df = read_only_df[read_only_df["Status"] == status_filter]
    st.dataframe(read_only_df.style.map(style_status, subset=["Status"]), use_container_width=True, hide_index=True)

render_local_attendance_and_files()
render_local_tools()
