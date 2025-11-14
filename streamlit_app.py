# import streamlit as st
# import json
# import os
# import copy
# from datetime import datetime, timezone
# from azure.storage.blob import BlobServiceClient

# # --- Configuration ---
# # This code now prioritizes an environment variable for the connection string,
# # which is the standard practice for self-hosted applications.
# AZURE_STORAGE_CONNECTION_STRING = st.secrets["AZURE_STORAGE_CONNECTION_STRING"]
# APP_METADATA_CONTAINER_NAME = "app-metadata"

# # Check if the connection string was loaded
# if not AZURE_STORAGE_CONNECTION_STRING:
#     st.error("Azure Storage Connection String is not configured. Please set the AZURE_STORAGE_CONNECTION_STRING environment variable.")
#     st.stop()


# # Set the page to wide layout. This must be the first Streamlit command.
# st.set_page_config(layout="wide")

# # --- Azure Blob Storage Functions ---

# @st.cache_data(ttl=300) # Cache for 5 minutes
# def download_latest_prompt_repo_from_blob():
#     """Downloads and parses the latest 'prompt_repo_*.json' blob from Azure."""
#     try:
#         blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
#         container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)

#         blob_list = list(container_client.list_blobs(name_starts_with="prompt_repo_"))
#         if not blob_list:
#             st.warning("No prompt repository found in Azure Blob Storage. Creating a default structure.")
#             return {"APPS": [{"name": "mmx", "prompts": []}]}

#         latest_blob = max(blob_list, key=lambda b: b.name)
#         st.info(f"Loading latest version: {latest_blob.name}")

#         blob_client = container_client.get_blob_client(latest_blob.name)
#         return json.loads(blob_client.download_blob().readall())
#     except Exception as e:
#         st.error(f"Failed to load data from Azure Blob Storage: {str(e)}")
#         return {"APPS": []}

# def upload_prompt_repo_to_blob(data_to_upload: dict):
#     """
#     Uploads a new timestamped prompt repository to Azure Blob Storage.
#     This version DOES NOT delete old blobs.
#     """
#     try:
#         blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
#         container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
#         timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
#         new_blob_name = f"prompt_repo_{timestamp}.json"

#         container_client.upload_blob(
#             name=new_blob_name,
#             data=json.dumps(data_to_upload, indent=4),
#             overwrite=True
#         )
#         st.success(f"Successfully uploaded to Azure as {new_blob_name}")
#         return True
#     except Exception as e:
#         st.error(f"Failed to upload to Azure Blob Storage: {str(e)}")
#         return False

# def fetch_previous_blobs():
#     """Fetches a list of all prompt repository versions from Azure Blob Storage."""
#     try:
#         blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
#         container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
#         blob_list = list(container_client.list_blobs(name_starts_with="prompt_repo_"))
#         blob_list.sort(key=lambda b: b.name, reverse=True)
#         return [blob.name for blob in blob_list]
#     except Exception as e:
#         st.error(f"Failed to fetch previous blobs: {str(e)}")
#         return []

# def load_blob_content_for_preview(blob_name):
#     """Loads and parses the content of a specific blob for previewing."""
#     try:
#         blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
#         container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
#         blob_client = container_client.get_blob_client(blob_name)
#         return json.loads(blob_client.download_blob().readall())
#     except Exception as e:
#         st.error(f"Failed to load blob {blob_name}: {str(e)}")
#         return None

# # --- UI Layout ---

# st.title("Prompt Repository Editor")

# if "prompt_data" not in st.session_state:
#     with st.spinner("Loading prompt repository from Azure..."):
#         st.session_state.prompt_data = download_latest_prompt_repo_from_blob()

# data = st.session_state.prompt_data

# if not data or not data.get("APPS"):
#     st.error("Could not load a valid prompt structure from Azure. Please check the connection string and container name.")
# else:
#     st.subheader("Edit Prompt Content")
#     prompt_list = data["APPS"][0].get("prompts", [])
#     prompt_names = [p.get("name", f"Unnamed Prompt {i}") for i, p in enumerate(prompt_list)]
#     if not prompt_names:
#         st.warning("No prompts found in the repository.")
#     selected_prompt_name = st.selectbox("Select a prompt to edit:", prompt_names)
#     selected_index = prompt_names.index(selected_prompt_name) if selected_prompt_name else -1
#     initial_content_str = ""
#     if selected_index != -1:
#         initial_content_str = "\n".join(prompt_list[selected_index].get("content", []))
#     edited_content_str = st.text_area("Prompt Content:", value=initial_content_str, height=400, key=f"editor_{selected_prompt_name}")

#     if st.button("Upload Changes to Azure"):
#         if selected_index != -1 and edited_content_str.strip() != initial_content_str.strip():
#             with st.spinner("Uploading to Azure..."):
#                 updated_data = copy.deepcopy(data)
#                 updated_data["APPS"][0]["prompts"][selected_index]["content"] = edited_content_str.split('\n')
#                 if upload_prompt_repo_to_blob(updated_data):
#                     st.cache_data.clear()
#                     st.session_state.prompt_data = updated_data
#                     st.rerun()
#         else:
#             st.info("No changes detected or no prompt selected.")
#     st.divider()
#     col1, col2 = st.columns(2)
#     with col1:
#         st.subheader("Previous Versions")
#         previous_blobs = fetch_previous_blobs()
#         if previous_blobs:
#             selected_blob = st.selectbox("Select a version to preview", previous_blobs)
#             if st.button("Preview Selected Version"):
#                 with st.spinner(f"Loading preview..."):
#                     st.session_state.preview_data = load_blob_content_for_preview(selected_blob)
#         if "preview_data" in st.session_state:
#             st.subheader("Preview")
#             st.json(st.session_state.preview_data, expanded=False)
#     with col2:
#         st.subheader("Raw JSON Editor")
#         edited_raw_json = st.text_area("Edit the full JSON object:", value=json.dumps(data, indent=2), height=450)
#         if st.button("Upload Raw JSON to Azure"):
#             try:
#                 new_data = json.loads(edited_raw_json)
#                 if upload_prompt_repo_to_blob(new_data):
#                     st.cache_data.clear()
#                     st.session_state.prompt_data = new_data
#                     st.rerun()
#             except json.JSONDecodeError:
#                 st.error("Invalid JSON format.")
import streamlit as st
import json
import copy
from datetime import datetime, timezone
from azure.storage.blob import BlobServiceClient

# Set the page to wide layout. This must be the first Streamlit command.
st.set_page_config(layout="wide")

# --- 🔐 PASSWORD PROTECTION ---

def check_password():
    """Returns True if the user is logged in, False otherwise."""
    
    # Check if the user is already logged in
    if st.session_state.get("logged_in", False):
        return True

    try:
        # Load the correct password from secrets
        correct_password = st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        st.error("Password is not configured. Please set APP_PASSWORD in Streamlit secrets.")
        st.stop()

    # Create a login form
    with st.form("login"):
        st.header("Login Required")
        st.write("Please enter the password to access the editor.")
        password_attempt = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        if password_attempt == correct_password:
            # If password is correct, set session state and rerun
            st.session_state.logged_in = True
            st.rerun()
        else:
            st.error("Incorrect password. Please try again.")
    
    # If not logged in, return False
    return False

# --- 🏃‍♂️ MAIN APP EXECUTION ---

# Stop execution if the password check fails
if not check_password():
    st.stop()

# --- Configuration ---
try:
    AZURE_STORAGE_CONNECTION_STRING = st.secrets["AZURE_STORAGE_CONNECTION_STRING"]
except (KeyError, FileNotFoundError):
    st.error("Azure Storage Connection String is not configured. Please set it in your Streamlit secrets.")
    st.stop()

# APP_METADATA_CONTAINER_NAME = "app-metadata"
# Define the list of applications the editor will manage.
SUPPORTED_APPS = ["mmx", "FAST", "salesmate","mmm1", "kythera"]

ENVIRONMENTS = ["dev", "qa", "prod"]
selected_env = st.sidebar.selectbox("Select Environment:", ENVIRONMENTS, index=0)

# Determine which container to use
if selected_env == "dev":
    APP_METADATA_CONTAINER_NAME = "app-metadata"
elif selected_env == "qa":
    APP_METADATA_CONTAINER_NAME = "app-metadata-qa"
else:
    APP_METADATA_CONTAINER_NAME = "app-metadata-prod"

st.sidebar.info(f"Active container: {APP_METADATA_CONTAINER_NAME}")

# Check if the connection string was loaded
if not AZURE_STORAGE_CONNECTION_STRING:
    st.error("Azure Storage Connection String is not configured. Please set the AZURE_STORAGE_CONNECTION_STRING environment variable or Streamlit secret.")
    st.stop()

# Set the page to wide layout. This must be the first Streamlit command.
st.set_page_config(layout="wide")

# --- Helper Functions ---
def get_blob_prefix(app_name: str) -> str:
    """Gets the correct blob name prefix based on the application name."""
    app_name_lower = app_name.lower()
    if app_name_lower == "mmx":
        return "prompt_repo_"
    return f"{app_name_lower}_prompt_repo_"

# --- Azure Blob Storage Functions ---

@st.cache_data(ttl=300) # Cache for 5 minutes
def download_latest_prompt_repo_from_blob(app_name: str,env: str):
    """Downloads and parses the latest prompt JSON blob for a specific app."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        prefix = get_blob_prefix(app_name)

        blob_list = list(container_client.list_blobs(name_starts_with=prefix))
        if not blob_list:
            st.warning(f"No prompt repository found for app '{app_name}'. You can initialize it using the Raw JSON editor below.")
            # Return a default structure with the APPS wrapper.
            return {"APPS": [{"name": app_name, "prompts": []}]}

        latest_blob = max(blob_list, key=lambda b: b.name)
        st.info(f"Loading latest version for '{app_name}': {latest_blob.name}")

        blob_client = container_client.get_blob_client(latest_blob.name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load data for '{app_name}' from Azure Blob Storage: {str(e)}")
        return {"APPS": []}

def upload_prompt_repo_to_blob(app_name: str, data_to_upload: dict):
    """Uploads a new timestamped prompt repository for a specific app."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        
        # Determine blob name based on app
        if app_name.lower() == "mmx":
            new_blob_name = f"prompt_repo_{timestamp}.json"
        else:
            new_blob_name = f"{app_name.lower()}_prompt_repo_{timestamp}.json"

        container_client.upload_blob(
            name=new_blob_name,
            data=json.dumps(data_to_upload, indent=4),
            overwrite=True
        )
        st.success(f"Successfully uploaded for '{app_name}' as {new_blob_name}")
        return True
    except Exception as e:
        st.error(f"Failed to upload to Azure Blob Storage: {str(e)}")
        return False

def fetch_previous_blobs(app_name: str):
    """Fetches a list of all prompt repository versions for a specific app."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        prefix = get_blob_prefix(app_name)
        
        blob_list = list(container_client.list_blobs(name_starts_with=prefix))
        blob_list.sort(key=lambda b: b.name, reverse=True)
        return [blob.name for blob in blob_list]
    except Exception as e:
        st.error(f"Failed to fetch previous blobs for '{app_name}': {str(e)}")
        return []

def load_blob_content_for_preview(blob_name: str):
    """Loads and parses the content of a specific blob for previewing."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(APP_METADATA_CONTAINER_NAME)
        blob_client = container_client.get_blob_client(blob_name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load blob {blob_name}: {str(e)}")
        return None

# --- UI Layout ---

st.title("Prompt Repository Editor")

# App selection is the primary driver of the UI
selected_app_name = st.selectbox("Select an App to manage:", SUPPORTED_APPS)

# When the app changes, clear the cache and reload data
if 'current_app' not in st.session_state or st.session_state.current_app != selected_app_name:
    st.cache_data.clear()
    st.session_state.current_app = selected_app_name
    if "preview_data" in st.session_state:
        del st.session_state.preview_data

with st.spinner(f"Loading data for '{selected_app_name}'..."):
    # `full_data` holds the entire JSON structure, e.g., {"APPS": [...]}
    full_data = download_latest_prompt_repo_from_blob(selected_app_name,selected_env)

# Find the specific app's data within the loaded structure
app_data = next((app for app in full_data.get("APPS", []) if app.get("name", "").lower() == selected_app_name.lower()), None)

st.subheader(f"Editing Prompts for: `{selected_app_name}`")

if app_data is None:
    st.warning(f"Could not find data for '{selected_app_name}' in the loaded file. You can initialize it below.")
    prompt_list = []
else:
    prompt_list = app_data.get("prompts", [])

prompt_names = [p.get("name", f"Unnamed Prompt {i}") for i, p in enumerate(prompt_list)]

if not prompt_names:
    st.warning(f"No prompts found for '{selected_app_name}'. You can add one via the Raw JSON Editor.")
else:
    selected_prompt_name = st.selectbox(
        "Select a prompt to edit:",
        prompt_names,
        key=f"prompt_select_{selected_app_name}"
    )
    selected_prompt_index = prompt_names.index(selected_prompt_name) if selected_prompt_name else -1

    if selected_prompt_index != -1:
        initial_content_str = "\n".join(prompt_list[selected_prompt_index].get("content", []))
        
        edited_content_str = st.text_area(
            "Prompt Content:",
            value=initial_content_str,
            height=400,
            key=f"editor_{selected_app_name}_{selected_prompt_name}"
        )

        if st.button("Upload Changes to Azure"):
            if edited_content_str.strip() != initial_content_str.strip():
                with st.spinner(f"Uploading changes for '{selected_app_name}'..."):
                    updated_data = copy.deepcopy(full_data)
                    # Find the app to update within the copied data structure
                    app_to_update = next((app for app in updated_data["APPS"] if app.get("name", "").lower() == selected_app_name.lower()), None)
                    if app_to_update:
                        app_to_update["prompts"][selected_prompt_index]["content"] = edited_content_str.split('\n')
                        if upload_prompt_repo_to_blob(selected_app_name, updated_data):
                            st.cache_data.clear()
                            st.rerun()
                    else:
                        st.error(f"Error: Could not find '{selected_app_name}' in the data structure to update.")
            else:
                st.info("No changes detected.")

st.divider()

# --- Raw JSON Editor and Version History ---
col1, col2 = st.columns(2)
with col1:
    st.subheader("Previous Versions")
    previous_blobs = fetch_previous_blobs(selected_app_name)
    if previous_blobs:
        selected_blob = st.selectbox("Select a version to preview", previous_blobs, key=f"version_select_{selected_app_name}")
        if st.button("Preview Selected Version"):
            with st.spinner(f"Loading preview for {selected_blob}..."):
                st.session_state.preview_data = load_blob_content_for_preview(selected_blob)
    else:
        st.info(f"No previous versions found for '{selected_app_name}'.")

    if "preview_data" in st.session_state and st.session_state.preview_data:
        st.subheader("Preview")
        st.json(st.session_state.preview_data, expanded=False)

with col2:
    st.subheader("Raw JSON Editor")
    # The template for a new or empty app now includes the APPS wrapper
    json_template = full_data
    if not full_data.get("APPS") or not any(app.get("name", "").lower() == selected_app_name.lower() for app in full_data["APPS"]):
        json_template = {
            "APPS": [
                {
                    "name": selected_app_name,
                    "prompts": [
                        {
                            "name": "EXAMPLE_PROMPT",
                            "description": "An example description.",
                            "location_identifier": "example.py/my_function()",
                            "content": [
                                "This is line 1 of the prompt content.",
                                "This is line 2."
                            ]
                        }
                    ]
                }
            ]
        }
    
    edited_raw_json = st.text_area(
        "Edit the full JSON object for this app:",
        value=json.dumps(json_template, indent=2),
        height=450,
        key=f"raw_json_{selected_app_name}"
    )
    if st.button("Upload Raw JSON to Azure"):
        try:
            new_data = json.loads(edited_raw_json)
            # Basic validation for the required structure
            if "APPS" not in new_data or not isinstance(new_data["APPS"], list):
                 st.error("Invalid JSON structure. Root must contain an 'APPS' list.")
            else:
                if upload_prompt_repo_to_blob(selected_app_name, new_data):
                    st.cache_data.clear()
                    st.rerun()
        except json.JSONDecodeError:
            st.error("Invalid JSON format. Please correct the syntax.")

