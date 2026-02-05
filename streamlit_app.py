import streamlit as st
import json
import copy
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from azure.storage.blob import BlobServiceClient
import requests
import difflib
import pandas as pd
from io import StringIO
import streamlit.components.v1 as components

# ==========================================
# --- CONFIGURATION ---
# ==========================================

SUPPORTED_APPS = ["mmx", "FAST", "insightsai", "mmm", "patient_claims", "fast1", "insightsai1","ihub","salesmate","access_iq"]
APP_NAME_ALIAS = {
    "fast": "fast1",
    "fast1": "fast"
}
FORCE_MIGRATION_READ_FROM_ALIAS = {}
ENVIRONMENTS = ["dev", "qa", "prod", "aws"]

# ==========================================
# --- HELPER FUNCTIONS ---
# ==========================================

def get_api_base_url(env):
    """Returns the backend API URL for the selected environment."""
    api_map = {
        "dev": "https://ciathciaidevbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io", 
        "qa": "https://ciathciaitstbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io",
        "prod": "https://ciathciaiprdbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io",
        "aws": "https://ciathena.info:8000"
    }
    return api_map.get(env, api_map["dev"])

def validate_metadata_json(data, target_app_name):
    """
    Validates that the JSON follows the strict structure required by metadata.py.
    Rule 1: Root must be a dictionary.
    Rule 2: Must contain a root key matching the Uppercase App Name (e.g., "MMM").
    Rule 3: The content of that key must be a list of tables.
    """
    if not isinstance(data, dict):
        return False, "❌ Invalid Root: The file must be a JSON object."

    # Logic from metadata.py: json_key = app_name.strip().upper()
    required_key = target_app_name.strip().upper()
    
    if required_key not in data:
        # Check if they used lowercase by mistake to give a helpful error
        found_keys = [k for k in data.keys() if k.lower() == target_app_name.lower()]
        if found_keys:
            return False, f"❌ Casing Error: Found key '{found_keys[0]}', but system requires ALL CAPS: '{required_key}'."
        return False, f"❌ Missing Root Key: JSON must contain the key '{required_key}' at the root."

    # Check if the content is a list (standard metadata structure)
    if not isinstance(data[required_key], list):
         return False, f"❌ Invalid Structure: The value for '{required_key}' must be a list of tables."

    return True, "✅ Valid"

def check_chroma_status(app_name, env):
    """Fetches the current background task status from the backend."""
    base_url = get_api_base_url(env)
    url = f"{base_url}/admin/chroma/status/{app_name}"
    
    try:
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return resp.json()
        return {"status": "unknown", "message": "Could not reach server."}
    except Exception:
        return {"status": "error", "message": "Connection failed."}

def trigger_cache_clear(app_name, env):
    """Calls backend to clear cache."""
    base_url = get_api_base_url(env)
    url = f"{base_url}/admin/cache/clear"
    
    try:
        with st.spinner(f"Clearing cache on {env.upper()}..."):
            resp = requests.post(
                url, 
                json={"app_name": app_name, "target": "all"},
                headers={"x-admin-token": "my-super-secret-admin-key-123"},
                timeout=5
            )
            if resp.status_code == 200:
                st.toast(f"Cache Cleared! ({resp.json().get('keys_deleted')} keys)", icon="✨")
            else:
                st.error(f"Cache Clear Failed: {resp.text}")
    except Exception as e:
        st.error(f"API Error: {str(e)}")

def trigger_chroma_populate(app_name, env):
    """Calls backend to populate ChromaDB."""
    base_url = get_api_base_url(env)
    url = f"{base_url}/admin/chroma/populate"
    
    try:
        with st.spinner(f"🚀 Triggering Chroma Population for {app_name} on {env.upper()}..."):
            resp = requests.post(
                url, 
                json={"app_name": app_name},
                headers={"x-admin-token": "my-super-secret-admin-key-123"},
                timeout=5
            )
            
            if resp.status_code == 200:
                st.success(f"✅ Job Started: {resp.json().get('message')}")
                st.info("The process is running in the background. It may take a few minutes to complete.")
            else:
                st.error(f"Failed to trigger: {resp.text}")
    except Exception as e:
        st.error(f"API Connection Error: {str(e)}")

def get_blob_prefix(app_name: str) -> str:
    """Gets the correct file prefix based on the application name."""
    app_name_lower = app_name.lower()
    if app_name_lower == "mmx":
        return "prompt_repo_"
    return f"{app_name_lower}_prompt_repo_"

def determine_read_app_name(ui_app_name: str) -> str:
    ui_app_name = ui_app_name.lower()
    if FORCE_MIGRATION_READ_FROM_ALIAS.get(ui_app_name, False):
        return APP_NAME_ALIAS.get(ui_app_name, ui_app_name)
    return ui_app_name

def determine_write_app_name(ui_app_name: str) -> str:
    return ui_app_name.lower()

# ==========================================
# --- AWS S3 FUNCTIONS ---
# ==========================================

def get_s3_client():
    """Create S3 client with error handling for missing credentials."""
    aws_key = st.session_state.get('AWS_ACCESS_KEY_ID')
    aws_secret = st.session_state.get('AWS_SECRET_ACCESS_KEY')
    aws_region = st.session_state.get('AWS_REGION')
    
    if not aws_key or not aws_secret or not aws_region:
        raise ValueError(
            "AWS credentials not configured. Please add AWS_ACCESS_KEY_ID, "
            "AWS_SECRET_ACCESS_KEY, and AWS_DEFAULT_REGION to your secrets.toml file."
        )
    
    return boto3.client(
        's3',
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        region_name=aws_region
    )

def download_latest_from_s3(app_name: str):
    """Downloads the latest prompt JSON from S3."""
    s3 = get_s3_client()
    prefix = get_blob_prefix(app_name)
    bucket = st.session_state.get('S3_BUCKET_NAME')
    
    try:
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        
        if 'Contents' not in response:
            fallback_prefix = "prompt_repo_"
            response = s3.list_objects_v2(Bucket=bucket, Prefix=fallback_prefix)

        if 'Contents' not in response:
            return {"APPS": [{"name": app_name, "prompts": []}]}

        latest_obj = max(response['Contents'], key=lambda x: x['Key'])
        file_key = latest_obj['Key']
        
        obj = s3.get_object(Bucket=bucket, Key=file_key)
        content = obj['Body'].read().decode('utf-8')
        return json.loads(content)

    except Exception as e:
        st.error(f"Failed to load from S3: {str(e)}")
        return {"APPS": []}

def upload_to_s3(app_name: str, data_to_upload: dict):
    """Uploads a new timestamped JSON to S3."""
    s3 = get_s3_client()
    bucket = st.session_state.get('S3_BUCKET_NAME')
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    if app_name.lower() == "mmx":
        new_key = f"prompt_repo_{timestamp}.json"
    else:
        new_key = f"{app_name.lower()}_prompt_repo_{timestamp}.json"

    try:
        s3.put_object(
            Bucket=bucket,
            Key=new_key,
            Body=json.dumps(data_to_upload, indent=4),
            ContentType='application/json'
        )
        st.success(f"Successfully uploaded to S3 as {new_key}")
        return True
    except Exception as e:
        st.error(f"Failed to upload to S3: {str(e)}")
        return False

def fetch_previous_from_s3(app_name: str):
    """Fetches list of file versions from S3."""
    s3 = get_s3_client()
    prefix = get_blob_prefix(app_name)
    bucket = st.session_state.get('S3_BUCKET_NAME')
    
    try:
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        
        if 'Contents' not in response:
             response = s3.list_objects_v2(Bucket=bucket, Prefix="prompt_repo_")

        if 'Contents' in response:
            sorted_files = sorted(response['Contents'], key=lambda x: x['Key'], reverse=True)
            return [obj['Key'] for obj in sorted_files]
        return []
    except Exception as e:
        st.error(f"Failed to fetch history from S3: {str(e)}")
        return []

def load_s3_preview(key: str):
    """Loads a specific S3 object for preview."""
    s3 = get_s3_client()
    bucket = st.session_state.get('S3_BUCKET_NAME')
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        content = obj['Body'].read().decode('utf-8')
        return json.loads(content)
    except Exception as e:
        st.error(f"Failed to load preview {key}: {str(e)}")
        return None

def download_metadata_from_s3(app_name: str):
    """Downloads metadata JSON from S3."""
    s3 = get_s3_client()
    bucket = st.session_state.get('S3_BUCKET_NAME')
    filename = f"{app_name.lower()}.json"
    
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"metadata/{filename}")
        return json.loads(obj['Body'].read().decode('utf-8'))
    except Exception as e:
        return None

def upload_metadata_to_s3(app_name: str, data: dict):
    """Uploads metadata JSON to S3."""
    s3 = get_s3_client()
    bucket = st.session_state.get('S3_BUCKET_NAME')
    filename = f"{app_name.lower()}.json"
    
    try:
        json_data = json.dumps(data, indent=4)
        s3.put_object(
            Bucket=bucket,
            Key=f"metadata/{filename}",
            Body=json_data,
            ContentType='application/json'
        )
        return True
    except Exception as e:
        st.error(f"Upload failed: {e}")
        return False

# ==========================================
# --- AZURE BLOB FUNCTIONS ---
# ==========================================

@st.cache_data(ttl=300)
def download_latest_from_azure(app_name: str, container_name: str, conn_str: str):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service_client.get_container_client(container_name)
        prefix = get_blob_prefix(app_name)

        blob_list = list(container_client.list_blobs(name_starts_with=prefix))
        if not blob_list:
            return {"APPS": [{"name": app_name, "prompts": []}]}

        latest_blob = max(blob_list, key=lambda b: b.name)
        blob_client = container_client.get_blob_client(latest_blob.name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load data from Azure: {str(e)}")
        return {"APPS": []}

def upload_to_azure(app_name: str, data_to_upload: dict, container_name: str, conn_str: str):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service_client.get_container_client(container_name)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        
        if app_name.lower() == "mmx":
            new_blob_name = f"prompt_repo_{timestamp}.json"
        else:
            new_blob_name = f"{app_name.lower()}_prompt_repo_{timestamp}.json"

        container_client.upload_blob(
            name=new_blob_name,
            data=json.dumps(data_to_upload, indent=4),
            overwrite=True
        )
        st.success(f"Successfully uploaded to Azure as {new_blob_name}")
        return True
    except Exception as e:
        st.error(f"Failed to upload to Azure: {str(e)}")
        return False

def fetch_previous_from_azure(app_name: str, container_name: str, conn_str: str):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service_client.get_container_client(container_name)
        prefix = get_blob_prefix(app_name)
        
        blob_list = list(container_client.list_blobs(name_starts_with=prefix))
        blob_list.sort(key=lambda b: b.name, reverse=True)
        return [blob.name for blob in blob_list]
    except Exception as e:
        st.error(f"Failed to fetch history from Azure: {str(e)}")
        return []

def load_azure_preview(blob_name: str, container_name: str, conn_str: str):
    try:
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        st.error(f"Failed to load blob {blob_name}: {str(e)}")
        return None

def download_metadata_from_azure(app_name: str, container_name: str, conn_str: str):
    """Downloads metadata JSON from Azure."""
    filename = f"{app_name.lower()}.json"
    try:
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(filename)
        
        if not blob_client.exists():
            return None
        
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        return None

def upload_metadata_to_azure(app_name: str, data: dict, container_name: str, conn_str: str):
    """Uploads metadata JSON to Azure."""
    filename = f"{app_name.lower()}.json"
    try:
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(filename)
        
        json_data = json.dumps(data, indent=4)
        blob_client.upload_blob(json_data, overwrite=True)
        return True
    except Exception as e:
        st.error(f"Upload failed: {e}")
        return False

# ==========================================
# --- DISPATCHER FUNCTIONS ---
# ==========================================

def download_data_dispatcher(app_name, env):
    if env == "aws":
        return download_latest_from_s3(app_name)
    else:
        container = st.session_state.get(f'container_{env}')
        conn_str = st.session_state.get('AZURE_STORAGE_CONNECTION_STRING')
        return download_latest_from_azure(app_name, container, conn_str)

def upload_data_dispatcher(app_name, data, env):
    if env == "aws":
        return upload_to_s3(app_name, data)
    else:
        container = st.session_state.get(f'container_{env}')
        conn_str = st.session_state.get('AZURE_STORAGE_CONNECTION_STRING')
        return upload_to_azure(app_name, data, container, conn_str)

def fetch_history_dispatcher(app_name, env):
    if env == "aws":
        return fetch_previous_from_s3(app_name)
    else:
        container = st.session_state.get(f'container_{env}')
        conn_str = st.session_state.get('AZURE_STORAGE_CONNECTION_STRING')
        return fetch_previous_from_azure(app_name, container, conn_str)

def preview_dispatcher(filename, env):
    if env == "aws":
        return load_s3_preview(filename)
    else:
        container = st.session_state.get(f'container_{env}')
        conn_str = st.session_state.get('AZURE_STORAGE_CONNECTION_STRING')
        return load_azure_preview(filename, container, conn_str)

def download_metadata_dispatcher(app_name, env):
    """Downloads metadata JSON from the appropriate storage."""
    if env == "aws":
        return download_metadata_from_s3(app_name)
    else:
        container = st.session_state.get(f'container_{env}')
        conn_str = st.session_state.get('AZURE_STORAGE_CONNECTION_STRING')
        return download_metadata_from_azure(app_name, container, conn_str)

def upload_metadata_dispatcher(app_name, data, env):
    """Uploads metadata JSON to the appropriate storage."""
    if env == "aws":
        return upload_metadata_to_s3(app_name, data)
    else:
        container = st.session_state.get(f'container_{env}')
        conn_str = st.session_state.get('AZURE_STORAGE_CONNECTION_STRING')
        return upload_metadata_to_azure(app_name, data, container, conn_str)

# ==========================================
# --- COMPARISON FUNCTIONS ---
# ==========================================

def get_prompt_dict(app_data):
    """Convert app prompts to a dictionary for easy comparison."""
    if not app_data or "prompts" not in app_data:
        return {}
    
    prompt_dict = {}
    for prompt in app_data.get("prompts", []):
        prompt_name = prompt.get("name", "")
        prompt_dict[prompt_name] = {
            "content": "\n".join(prompt.get("content", [])),
            "description": prompt.get("description", ""),
            "location_identifier": prompt.get("location_identifier", "")
        }
    return prompt_dict

def compare_prompts(env1_data, env2_data, env1_name, env2_name):
    """Compare prompts between two environments."""
    env1_prompts = get_prompt_dict(env1_data)
    env2_prompts = get_prompt_dict(env2_data)
    
    all_prompt_names = set(env1_prompts.keys()) | set(env2_prompts.keys())
    
    comparison_results = []
    
    for prompt_name in sorted(all_prompt_names):
        in_env1 = prompt_name in env1_prompts
        in_env2 = prompt_name in env2_prompts
        
        if not in_env1:
            comparison_results.append({
                "prompt_name": prompt_name,
                "status": f"Only in {env2_name}",
                "changes": 1,
                "details": f"This prompt exists only in {env2_name}"
            })
        elif not in_env2:
            comparison_results.append({
                "prompt_name": prompt_name,
                "status": f"Only in {env1_name}",
                "changes": 1,
                "details": f"This prompt exists only in {env1_name}"
            })
        else:
            # Compare content
            content1 = env1_prompts[prompt_name]["content"]
            content2 = env2_prompts[prompt_name]["content"]
            
            if content1 == content2:
                comparison_results.append({
                    "prompt_name": prompt_name,
                    "status": "Identical",
                    "changes": 0,
                    "details": "No differences found"
                })
            else:
                # Count line differences
                diff = list(difflib.unified_diff(
                    content1.splitlines(keepends=True),
                    content2.splitlines(keepends=True),
                    lineterm=''
                ))
                changes_count = sum(1 for line in diff if line.startswith('+') or line.startswith('-'))
                
                comparison_results.append({
                    "prompt_name": prompt_name,
                    "status": "Modified",
                    "changes": changes_count,
                    "details": f"{changes_count} line changes detected",
                    "diff": diff,
                    "content1": content1,
                    "content2": content2
                })
    
    return comparison_results

def push_prompts_between_envs(app_name, source_env, target_env):
    """
    Push all prompts from source environment to target environment.
    Works exactly like the Raw JSON upload in Prompt Editor.
    """
    try:
        read_app_name = determine_read_app_name(app_name)
        write_app_name = determine_write_app_name(app_name)
        
        st.info(f"🔍 Reading from app: `{read_app_name}`, Writing as: `{write_app_name}`")
        
        st.info(f"📥 Step 1: Downloading data from {source_env.upper()}...")
        source_data = download_data_dispatcher(read_app_name, source_env)
        
        if not source_data:
            st.error(f"❌ No data returned from {source_env.upper()}")
            return False
            
        if "APPS" not in source_data:
            st.error(f"❌ No 'APPS' key found in {source_env.upper()} data")
            st.json(source_data)
            return False
        
        if not isinstance(source_data.get("APPS"), list):
            st.error("❌ Invalid JSON structure in source. 'APPS' must be a list.")
            return False
        
        st.success(f"✅ Downloaded data from {source_env.upper()}")
        
        app_found = False
        for app in source_data.get("APPS", []):
            if app.get("name", "").lower() == read_app_name.lower():
                app_found = True
                num_prompts = len(app.get("prompts", []))
                st.info(f"✅ Found app '{read_app_name}' with {num_prompts} prompts")
                break
        
        if not app_found:
            st.error(f"❌ App '{app_name}' not found in {source_env.upper()}")
            st.info(f"Available apps: {[app.get('name') for app in source_data.get('APPS', [])]}")
            return False
        
        st.info(f"📋 Step 2: Creating deep copy of source data...")
        target_data = copy.deepcopy(source_data)
        
        st.info(f"✏️ Step 3: Updating app names to '{write_app_name}'...")
        for app in target_data.get("APPS", []):
            if app.get("name", "").lower() == read_app_name.lower():
                app["name"] = write_app_name
        
        app_data = next(
            (app for app in target_data["APPS"] if app.get("name", "").lower() == write_app_name.lower()), 
            None
        )
        num_prompts = len(app_data.get("prompts", [])) if app_data else 0
        
        st.info(f"📤 Step 4: Uploading {num_prompts} prompts to {target_env.upper()}...")
        
        success = upload_data_dispatcher(write_app_name, target_data, target_env)
        
        if success:
            st.info(f"🧹 Step 5: Clearing cache for {target_env.upper()}...")
            trigger_cache_clear(write_app_name, target_env)
            st.success(f"✅ Successfully pushed {num_prompts} prompts from {source_env.upper()} to {target_env.upper()}!")
            return True
        else:
            st.error(f"❌ Upload failed for {target_env.upper()}")
            return False
            
    except Exception as e:
        st.error(f"❌ Error during push operation: {str(e)}")
        import traceback
        st.code(traceback.format_exc())
        return False

# ==========================================
# --- PASSWORD PROTECTION ---
# ==========================================

def check_password():
    """Returns True if the user is logged in, False otherwise."""
    if st.session_state.get("logged_in", False):
        return True

    try:
        correct_password = st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        st.error("Password is not configured. Please set APP_PASSWORD in Streamlit secrets.")
        st.stop()

    with st.form("login"):
        st.header("Login Required")
        st.write("Please enter the password to access the application.")
        password_attempt = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        if password_attempt == correct_password:
            st.session_state.logged_in = True
            st.rerun()
        else:
            st.error("Incorrect password. Please try again.")
    
    return False

# ==========================================
# --- LOAD CONFIGURATION ---
# ==========================================

def load_configuration():
    """Load Azure and AWS configuration from secrets."""
    try:
        # Azure Configuration (Required)
        st.session_state['AZURE_STORAGE_CONNECTION_STRING'] = st.secrets["AZURE_STORAGE_CONNECTION_STRING"]
        st.session_state['container_dev'] = "app-metadata"
        st.session_state['container_qa'] = "app-metadata-qa"
        st.session_state['container_prod'] = "app-metadata-prod"
        
        # AWS Configuration (Optional)
        st.session_state['AWS_ACCESS_KEY_ID'] = st.secrets.get("AWS_ACCESS_KEY_ID", "")
        st.session_state['AWS_SECRET_ACCESS_KEY'] = st.secrets.get("AWS_SECRET_ACCESS_KEY", "")
        st.session_state['AWS_REGION'] = st.secrets.get("AWS_DEFAULT_REGION", "")
        st.session_state['S3_BUCKET_NAME'] = st.secrets.get("S3_BUCKET_NAME", "")
        
    except (KeyError, FileNotFoundError) as e:
        st.error(f"Missing configuration in secrets: {e}")
        st.stop()

# ==========================================
# --- PAGE: PROMPT EDITOR ---
# ==========================================

def page_prompt_editor():
    st.title("📝 Prompt Repository Editor")
    
    col1, col2 = st.columns([1, 3])
    with col1:
        selected_env = st.selectbox("Environment:", ENVIRONMENTS, index=0, key="editor_env")
    with col2:
        selected_app_name = st.selectbox("App:", SUPPORTED_APPS, key="editor_app")
    
    # Check if AWS is selected but not configured
    if selected_env == "aws":
        aws_configured = (
            st.session_state.get('AWS_ACCESS_KEY_ID') and 
            st.session_state.get('AWS_SECRET_ACCESS_KEY') and 
            st.session_state.get('AWS_REGION') and
            st.session_state.get('S3_BUCKET_NAME')
        )
        
        if not aws_configured:
            st.error("❌ AWS Environment Not Configured")
            st.warning("""
            AWS credentials are missing. To use the AWS environment, add the following to your `.streamlit/secrets.toml` file:
            
            ```toml
            AWS_ACCESS_KEY_ID = "your-aws-access-key-id"
            AWS_SECRET_ACCESS_KEY = "your-aws-secret-access-key"
            AWS_DEFAULT_REGION = "us-east-1"
            S3_BUCKET_NAME = "your-s3-bucket-name"
            ```
            
            **Note:** If you don't use AWS, select DEV, QA, or PROD environment instead.
            """)
            st.info("💡 **Tip:** AWS environment is optional. You can use DEV, QA, or PROD with Azure Storage.")
            st.stop()
    
    read_app_name = determine_read_app_name(selected_app_name)
    write_app_name = determine_write_app_name(selected_app_name)
    
    state_key = f"{read_app_name}_{selected_env}"
    if 'current_app_state' not in st.session_state or st.session_state.current_app_state != state_key:
        st.cache_data.clear()
        st.session_state.current_app_state = state_key
        if "preview_data" in st.session_state:
            del st.session_state.preview_data
    
    with st.spinner(f"Loading data for '{selected_app_name}' from {selected_env.upper()}..."):
        full_data = download_data_dispatcher(read_app_name, selected_env)
    
    app_data = None
    if full_data and "APPS" in full_data:
        app_data = next((app for app in full_data["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)
    
    st.caption(f"📥 Loading from: `{read_app_name}` | 📤 Saving as: `{write_app_name}`")
    
    if FORCE_MIGRATION_READ_FROM_ALIAS.get(selected_app_name.lower(), False):
        st.warning("⚠️ Migration mode ON — loading from alias source")
    
    if app_data is None:
        st.warning(f"Could not find data for '{selected_app_name}'. You can initialize it via Raw JSON Editor.")
        prompt_list = []
    else:
        prompt_list = app_data.get("prompts", [])
    
    prompt_names = [p.get("name", f"Unnamed Prompt {i}") for i, p in enumerate(prompt_list)]
    
    # Prompt Editor
    if not prompt_names:
        st.warning(f"No prompts found for '{selected_app_name}'. Add one via the Raw JSON Editor.")
    else:
        selected_prompt_name = st.selectbox("Select prompt:", prompt_names, key=f"prompt_select_{selected_app_name}")
        selected_prompt_index = prompt_names.index(selected_prompt_name) if selected_prompt_name else -1
        
        if selected_prompt_index != -1:
            initial_content_str = "\n".join(prompt_list[selected_prompt_index].get("content", []))
            
            edited_content_str = st.text_area(
                "Prompt Content:",
                value=initial_content_str,
                height=400,
                key=f"editor_{selected_app_name}_{selected_prompt_name}"
            )
            
            if st.button("💾 Upload Changes", type="primary"):
                if edited_content_str.strip() != initial_content_str.strip():
                    with st.spinner(f"Uploading changes for '{selected_app_name}'..."):
                        updated_data = copy.deepcopy(full_data)
                        
                        if "APPS" not in updated_data:
                             updated_data = {"APPS": [{"name": selected_app_name, "prompts": []}]}
                        
                        app_to_update = next((app for app in updated_data["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)
                        
                        if app_to_update:
                            app_to_update["prompts"][selected_prompt_index]["content"] = edited_content_str.split('\n')
                            app_to_update["name"] = write_app_name
                            
                            if upload_data_dispatcher(write_app_name, updated_data, selected_env):
                                trigger_cache_clear(write_app_name, selected_env)
                                st.cache_data.clear()
                                st.rerun()
                        else:
                            st.error(f"Error: Structure mismatch during save.")
                else:
                    st.info("No changes detected.")
    
    st.divider()
    
    # Previous Versions & Raw JSON Editor
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("📚 Previous Versions")
        previous_blobs = fetch_history_dispatcher(read_app_name, selected_env)
        
        if previous_blobs:
            selected_blob = st.selectbox("Select version:", previous_blobs, key=f"version_select_{selected_app_name}")
            if st.button("👁️ Preview Selected Version"):
                with st.spinner(f"Loading preview for {selected_blob}..."):
                    st.session_state.preview_data = preview_dispatcher(selected_blob, selected_env)
        else:
            st.info(f"No previous versions found for '{selected_app_name}'.")
        
        if "preview_data" in st.session_state and st.session_state.preview_data:
            st.subheader("Preview")
            st.json(st.session_state.preview_data, expanded=False)
    
    with col2:
        st.subheader("⚙️ Raw JSON Editor")
        json_template = full_data
        
        has_app = False
        if full_data and "APPS" in full_data:
             has_app = any(app.get("name", "").lower() == selected_app_name.lower() for app in full_data["APPS"])
        
        if not has_app:
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
                                    "This is line 1.",
                                    "This is line 2."
                                ]
                            }
                        ]
                    }
                ]
            }
        
        edited_raw_json = st.text_area(
            "Edit full JSON:",
            value=json.dumps(json_template, indent=2),
            height=450,
            key=f"raw_json_{selected_app_name}"
        )
        
        if st.button("📤 Upload Raw JSON", type="primary"):
            try:
                new_data = json.loads(edited_raw_json)
                if "APPS" not in new_data or not isinstance(new_data["APPS"], list):
                     st.error("Invalid JSON structure. Root must contain an 'APPS' list.")
                else:
                    for app in new_data.get("APPS", []):
                        if app.get("name", "").lower() == read_app_name.lower():
                            app["name"] = write_app_name
                    if upload_data_dispatcher(write_app_name, new_data, selected_env):
                        trigger_cache_clear(write_app_name, selected_env)
                        st.cache_data.clear()
                        st.rerun()
            except json.JSONDecodeError:
                st.error("Invalid JSON format. Please correct the syntax.")

# ==========================================
# --- PAGE: ENVIRONMENT COMPARISON ---
# ==========================================

def page_environment_comparison():
    st.title("🔍 Environment Comparison")
    st.write("Compare prompt differences between environments to identify changes before deployment.")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        selected_app = st.selectbox("Select App:", SUPPORTED_APPS, key="compare_app")
    
    with col2:
        env1 = st.selectbox("Environment 1:", ENVIRONMENTS, index=0, key="compare_env1")
    
    with col3:
        env2 = st.selectbox("Environment 2:", ENVIRONMENTS, index=1, key="compare_env2")
    
    # Check if AWS is selected but not configured
    aws_configured = (
        st.session_state.get('AWS_ACCESS_KEY_ID') and 
        st.session_state.get('AWS_SECRET_ACCESS_KEY') and 
        st.session_state.get('AWS_REGION') and
        st.session_state.get('S3_BUCKET_NAME')
    )
    
    if (env1 == "aws" or env2 == "aws") and not aws_configured:
        st.error("❌ AWS Environment Not Configured")
        st.warning("""
        AWS credentials are missing. To compare with AWS environment, add the following to your `.streamlit/secrets.toml` file:
        
        ```toml
        AWS_ACCESS_KEY_ID = "your-aws-access-key-id"
        AWS_SECRET_ACCESS_KEY = "your-aws-secret-access-key"
        AWS_DEFAULT_REGION = "us-east-1"
        S3_BUCKET_NAME = "your-s3-bucket-name"
        ```
        
        **Alternatively:** Select only DEV, QA, or PROD environments for comparison.
        """)
        st.stop()
    
    # Add two buttons side by side: Run Comparison and Push Prompts
    button_col1, button_col2 = st.columns(2)
    
    with button_col1:
        run_comparison_clicked = st.button("🔄 Run Comparison", type="primary", use_container_width=True)
    
    with button_col2:
        push_button_text = f"🚀 Push {env1.upper()} → {env2.upper()}"
        push_prompts_clicked = st.button(push_button_text, type="secondary", use_container_width=True)
    
    # Initialize session state for push confirmation
    if 'show_push_confirm' not in st.session_state:
        st.session_state.show_push_confirm = False
    
    # Handle Push Prompts button click
    if push_prompts_clicked:
        if env1 == env2:
            st.warning("⚠️ Cannot push to the same environment. Please select two different environments.")
        else:
            st.session_state.show_push_confirm = True
    
    # Show confirmation dialog if needed
    if st.session_state.get('show_push_confirm', False):
        st.warning(f"⚠️ **Confirmation Required**")
        st.write(f"You are about to push **ALL prompts** from **{env1.upper()}** to **{env2.upper()}** for app **{selected_app}**.")
        st.write("This will **overwrite** the existing prompts in the target environment.")
        
        confirm_col1, confirm_col2, confirm_col3 = st.columns([1, 1, 2])
        
        with confirm_col1:
            if st.button(f"✅ Confirm Push", key="confirm_push", type="primary"):
                st.session_state.show_push_confirm = False
                with st.spinner(f"Pushing prompts from {env1.upper()} to {env2.upper()}..."):
                    success = push_prompts_between_envs(selected_app, env1, env2)
                    if success:
                        st.balloons()
                        st.cache_data.clear()
                        st.rerun()
        
        with confirm_col2:
            if st.button("❌ Cancel", key="cancel_push"):
                st.session_state.show_push_confirm = False
                st.rerun()
    
    # Handle Run Comparison button click
    if run_comparison_clicked:
        if env1 == env2:
            st.warning("Please select two different environments to compare.")
        else:
            with st.spinner(f"Loading data from {env1.upper()} and {env2.upper()}..."):
                read_app_name = determine_read_app_name(selected_app)
                
                data1 = download_data_dispatcher(read_app_name, env1)
                data2 = download_data_dispatcher(read_app_name, env2)
                
                app_data1 = None
                app_data2 = None
                
                if data1 and "APPS" in data1:
                    app_data1 = next((app for app in data1["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)
                
                if data2 and "APPS" in data2:
                    app_data2 = next((app for app in data2["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)
                
                comparison_results = compare_prompts(app_data1, app_data2, env1.upper(), env2.upper())
                
                st.subheader(f"📊 Comparison Summary: {selected_app}")
                st.caption(f"Comparing **{env1.upper()}** vs **{env2.upper()}**")
                
                total_prompts = len(comparison_results)
                modified = sum(1 for r in comparison_results if r["status"] == "Modified")
                only_env1 = sum(1 for r in comparison_results if r["status"] == f"Only in {env1.upper()}")
                only_env2 = sum(1 for r in comparison_results if r["status"] == f"Only in {env2.upper()}")
                identical = sum(1 for r in comparison_results if r["status"] == "Identical")
                
                metric_cols = st.columns(5)
                metric_cols[0].metric("Total Prompts", total_prompts)
                metric_cols[1].metric("Modified", modified, delta=None if modified == 0 else f"{modified}")
                metric_cols[2].metric(f"Only {env1.upper()}", only_env1)
                metric_cols[3].metric(f"Only {env2.upper()}", only_env2)
                metric_cols[4].metric("Identical", identical)
                
                st.divider()
                
                st.subheader("📋 Detailed Comparison")
                
                summary_df = pd.DataFrame([
                    {
                        "Prompt Name": r["prompt_name"],
                        "Status": r["status"],
                        "Changes": r["changes"]
                    }
                    for r in comparison_results
                ])
                
                def highlight_status(row):
                    if row['Status'] == 'Modified':
                        return ['background-color: #FFF9C4; color: #000000'] * len(row)
                    elif 'Only in' in row['Status']:
                        return ['background-color: #FFCDD2; color: #000000'] * len(row)
                    else:
                        return ['background-color: #C8E6C9; color: #000000'] * len(row)
                
                styled_df = summary_df.style.apply(highlight_status, axis=1).set_properties(**{
                    'color': 'black',
                    'background-color': 'white',
                    'border': '1px solid #ddd'
                })
                
                st.dataframe(styled_df, use_container_width=True, height=400)
                
                csv = summary_df.to_csv(index=False)
                st.download_button(
                    label="📥 Download Comparison Report (CSV)",
                    data=csv,
                    file_name=f"comparison_{selected_app}_{env1}_vs_{env2}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv"
                )
                
                st.divider()
                
                st.subheader("🔎 Detailed Differences")
                
                modified_prompts = [r for r in comparison_results if r["status"] == "Modified"]
                
                if modified_prompts:
                    for result in modified_prompts:
                        with st.expander(f"**{result['prompt_name']}** - {result['changes']} changes"):
                            col_a, col_b = st.columns(2)
                            
                            with col_a:
                                st.caption(f"**{env1.upper()}**")
                                st.code(result.get("content1", ""), language="text")
                            
                            with col_b:
                                st.caption(f"**{env2.upper()}**")
                                st.code(result.get("content2", ""), language="text")
                            
                            st.caption("**Unified Diff:**")
                            diff_text = "\n".join(result.get("diff", []))
                            st.code(diff_text, language="diff")
                else:
                    st.info("No modified prompts found.")

# ==========================================
# --- PAGE: EXCEL TO JSON CONVERTER ---
# ==========================================

def page_excel_converter():
    st.title("📊 Excel to JSON Converter")
    st.write("Upload Excel files and convert them to structured JSON format.")
    
    try:
        with open('excel_converter.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        components.html(html_content, height=1200, scrolling=True)
        
    except FileNotFoundError:
        st.error("❌ Excel converter HTML file not found!")
        st.write("Please ensure `excel_converter.html` is in the same directory as this app.")
        st.write("Expected location: `excel_converter.html`")
        
        st.markdown("---")
        st.subheader("Setup Instructions:")
        st.markdown("""
        1. Download the `excel_converter.html` file from the repository
        2. Place it in the same folder as `streamlit_app.py`
        3. Refresh this page
        
        **File structure should be:**
        ```
        your-repo/
        ├── streamlit_app.py
        ├── excel_converter.html    ← Add this file
        └── requirements.txt
        ```
        """)

# ==========================================
# --- PAGE: METADATA & CHROMADB MANAGER ---
# ==========================================

def page_metadata_manager():
    st.title("🗄️ Metadata & ChromaDB Manager")
    st.write("Manage metadata files and trigger ChromaDB indexing for your applications.")
    
    col1, col2 = st.columns([1, 3])
    with col1:
        selected_env = st.selectbox("Environment:", ENVIRONMENTS, index=0, key="metadata_env")
    with col2:
        selected_app_name = st.selectbox("App:", SUPPORTED_APPS, key="metadata_app")
    
    # Check if AWS is selected but not configured
    if selected_env == "aws":
        aws_configured = (
            st.session_state.get('AWS_ACCESS_KEY_ID') and 
            st.session_state.get('AWS_SECRET_ACCESS_KEY') and 
            st.session_state.get('AWS_REGION') and
            st.session_state.get('S3_BUCKET_NAME')
        )
        
        if not aws_configured:
            st.error("❌ AWS Environment Not Configured")
            st.warning("""
            AWS credentials are missing. To use the AWS environment, add the following to your `.streamlit/secrets.toml` file:
            
            ```toml
            AWS_ACCESS_KEY_ID = "your-aws-access-key-id"
            AWS_SECRET_ACCESS_KEY = "your-aws-secret-access-key"
            AWS_DEFAULT_REGION = "us-east-1"
            S3_BUCKET_NAME = "your-s3-bucket-name"
            ```
            
            **Note:** If you don't use AWS, select DEV, QA, or PROD environment instead.
            """)
            st.info("💡 **Tip:** AWS environment is optional. You can use DEV, QA, or PROD with Azure Storage.")
            st.stop()
    
    read_app_name = determine_read_app_name(selected_app_name)
    write_app_name = determine_write_app_name(selected_app_name)
    
    st.caption(f"📥 Loading from: `{read_app_name}` | 📤 Saving as: `{write_app_name}`")
    
    # Get container name for display
    if selected_env == "aws":
        storage_location = st.session_state.get('S3_BUCKET_NAME', 'S3 Bucket')
    else:
        storage_location = st.session_state.get(f'container_{selected_env}', 'Azure Container')
    
    st.divider()
    
    # Load Metadata
    with st.spinner("Loading metadata..."):
        metadata_json = download_metadata_dispatcher(read_app_name, selected_env)
    
    col_edit, col_actions = st.columns([3, 1])
    
    with col_edit:
        st.subheader("📝 Metadata JSON Editor")
        st.markdown(f"**File:** `{read_app_name.lower()}.json` in `{storage_location}`")
        
        if metadata_json is None:
            st.warning("⚠️ Metadata file not found. Creating new template.")
            json_str = json.dumps({selected_app_name.upper(): []}, indent=4)
        else:
            json_str = json.dumps(metadata_json, indent=4)
        
        edited_meta = st.text_area(
            "Edit Metadata JSON:",
            value=json_str,
            height=600,
            key=f"metadata_editor_{selected_app_name}"
        )
        
        if st.button("💾 Save Metadata", type="primary", use_container_width=True):
            try:
                data_to_save = json.loads(edited_meta)
                is_valid, message = validate_metadata_json(data_to_save, read_app_name)
                
                if not is_valid:
                    st.error(message)
                else:
                    if upload_metadata_dispatcher(write_app_name, data_to_save, selected_env):
                        st.success(f"✅ Metadata uploaded successfully! ({message})")
                        trigger_cache_clear(write_app_name, selected_env)
                        st.rerun()
                    else:
                        st.error("❌ Upload failed due to connection error.")
            except json.JSONDecodeError as e:
                st.error(f"❌ Invalid JSON Format: {str(e)}")
    
    with col_actions:
        st.subheader("🤖 ChromaDB Actions")
        
        st.info("""
        **Workflow:**
        1. Edit & Save metadata JSON on the left
        2. Click "Start ChromaDB Job" below
        3. Backend will re-index ChromaDB with updated metadata
        """)
        
        st.divider()
        
        # ChromaDB Population Button
        if st.button("🚀 Start ChromaDB Job", type="primary", use_container_width=True):
            trigger_chroma_populate(write_app_name, selected_env)
        
        st.divider()
        
        # Status Display
        st.markdown("**Job Status**")
        
        if st.button("🔄 Refresh Status", use_container_width=True):
            status_data = check_chroma_status(write_app_name, selected_env)
            
            state = status_data.get("status", "idle")
            msg = status_data.get("message", "")
            time_log = status_data.get("timestamp", "")
            
            if state == "running":
                st.warning(f"🟡 **Running**\n\n{msg}")
            elif state == "completed":
                st.success(f"🟢 **Completed**\n\n{msg}")
                if time_log:
                    st.caption(f"Finished: {time_log}")
            elif state == "failed":
                st.error(f"🔴 **Failed**\n\nError: {msg}")
            else:
                st.info(f"⚪ **Idle**\n\n{msg}")
        
        st.divider()
        
        # Cache Clear Button
        if st.button("🧹 Clear Cache", use_container_width=True):
            trigger_cache_clear(write_app_name, selected_env)
    
    # Metadata Structure Help
    with st.expander("ℹ️ Metadata JSON Structure Help"):
        st.markdown("""
        ### Required Structure
        
        The metadata JSON must follow this structure:
        
        ```json
        {
          "APP_NAME": [
            {
              "table_name": "example_table",
              "description": "Table description",
              "columns": [
                {
                  "name": "column1",
                  "type": "VARCHAR",
                  "description": "Column description"
                }
              ]
            }
          ]
        }
        ```
        
        ### Important Rules:
        
        1. **Root Key:** Must be the app name in UPPERCASE (e.g., "MMX", "FAST")
        2. **Root Value:** Must be a list of table objects
        3. **Casing:** The app name key must be in ALL CAPS
        
        ### Example for MMX app:
        
        ```json
        {
          "MMX": [
            {
              "table_name": "users",
              "description": "User information table",
              "columns": [
                {
                  "name": "user_id",
                  "type": "INTEGER",
                  "description": "Unique user identifier"
                },
                {
                  "name": "username",
                  "type": "VARCHAR",
                  "description": "User login name"
                }
              ]
            }
          ]
        }
        ```
        """)

# ==========================================
# --- MAIN APPLICATION ---
# ==========================================

def main():
    st.set_page_config(
        page_title="Prompt Management Suite",
        page_icon="🤖",
        layout="wide"
    )
    
    # Password protection
    if not check_password():
        st.stop()
    
    # Load configuration
    load_configuration()
    
    # Sidebar navigation
    st.sidebar.title("🤖 Prompt Management Suite")
    st.sidebar.markdown("---")
    
    page = st.sidebar.radio(
        "Navigate to:",
        [
            "📝 Prompt Editor",
            "🔍 Environment Comparison",
            "📊 Excel to JSON Converter",
            "🗄️ Metadata & ChromaDB Manager"
        ]
    )
    
    st.sidebar.markdown("---")
    st.sidebar.caption("Logged in as: **Admin**")
    
    # Route to appropriate page
    if page == "📝 Prompt Editor":
        page_prompt_editor()
    elif page == "🔍 Environment Comparison":
        page_environment_comparison()
    elif page == "📊 Excel to JSON Converter":
        page_excel_converter()
    elif page == "🗄️ Metadata & ChromaDB Manager":
        page_metadata_manager()

if __name__ == "__main__":
    main()
