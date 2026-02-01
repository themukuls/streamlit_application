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

def trigger_cache_clear(app_name, env):
    """Calls backend to clear cache."""
    api_map = {
        "dev": "https://ciathciaidevbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io", 
        "qa": "https://ciathciaitstbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io",
        "prod": "https://ciathciaiprdbeca01.thankfulisland-b0727d92.eastus2.azurecontainerapps.io",
        "aws": "https://ciathena.info:8000"
    }
    
    base_url = api_map.get(env, api_map["dev"])
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
    return boto3.client(
        's3',
        aws_access_key_id=st.session_state.get('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=st.session_state.get('AWS_SECRET_ACCESS_KEY'),
        region_name=st.session_state.get('AWS_REGION')
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
        # Azure Configuration
        st.session_state['AZURE_STORAGE_CONNECTION_STRING'] = st.secrets["AZURE_STORAGE_CONNECTION_STRING"]
        st.session_state['container_dev'] = "app-metadata"
        st.session_state['container_qa'] = "app-metadata-qa"
        st.session_state['container_prod'] = "app-metadata-prod"
        
        # AWS Configuration
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
    
    if st.button("🔄 Run Comparison", type="primary"):
        if env1 == env2:
            st.warning("Please select two different environments to compare.")
        else:
            with st.spinner(f"Loading data from {env1.upper()} and {env2.upper()}..."):
                read_app_name = determine_read_app_name(selected_app)
                
                # Load data from both environments
                data1 = download_data_dispatcher(read_app_name, env1)
                data2 = download_data_dispatcher(read_app_name, env2)
                
                # Extract app-specific data
                app_data1 = None
                app_data2 = None
                
                if data1 and "APPS" in data1:
                    app_data1 = next((app for app in data1["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)
                
                if data2 and "APPS" in data2:
                    app_data2 = next((app for app in data2["APPS"] if app.get("name", "").lower() == read_app_name.lower()), None)
                
                # Run comparison
                comparison_results = compare_prompts(app_data1, app_data2, env1.upper(), env2.upper())
                
                # Display summary
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
                
                # Display detailed results
                st.subheader("📋 Detailed Comparison")
                
                # Create summary table
                summary_df = pd.DataFrame([
                    {
                        "Prompt Name": r["prompt_name"],
                        "Status": r["status"],
                        "Changes": r["changes"]
                    }
                    for r in comparison_results
                ])
                
                # Color-code the status
                def highlight_status(row):
                    if row['Status'] == 'Modified':
                        return ['background-color: #fff3cd'] * len(row)
                    elif 'Only in' in row['Status']:
                        return ['background-color: #f8d7da'] * len(row)
                    else:
                        return ['background-color: #d4edda'] * len(row)
                
                styled_df = summary_df.style.apply(highlight_status, axis=1)
                st.dataframe(styled_df, use_container_width=True)
                
                # Export option
                csv = summary_df.to_csv(index=False)
                st.download_button(
                    label="📥 Download Comparison Report (CSV)",
                    data=csv,
                    file_name=f"comparison_{selected_app}_{env1}_vs_{env2}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv"
                )
                
                st.divider()
                
                # Detailed diff view for modified prompts
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
    
    # Embed the HTML converter
    html_content = """
    <!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Excel to JSON Converter</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/xlsx/dist/xlsx.full.min.js"></script>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; }
        .toast-container { position: fixed; top: 1rem; right: 1rem; z-index: 1000; }
        .toast { padding: 0.75rem 1rem; border-radius: 0.375rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 0.5rem; min-width: 250px; }
        .toast-success { background-color: #10b981; color: white; }
        .toast-error { background-color: #ef4444; color: white; }
        .toast-warning { background-color: #f59e0b; color: white; }
    </style>
</head>
<body class="bg-gray-50">
    <div id="toast-container" class="toast-container"></div>
    
    <div class="max-w-6xl mx-auto p-8">
        <div class="bg-white rounded-lg shadow-md p-6 mb-6">
            <h2 class="text-2xl font-bold mb-4">Upload Excel File</h2>
            <div id="file-upload-area" class="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center cursor-pointer hover:border-blue-500 transition">
                <input type="file" id="file-input" accept=".xlsx,.xls" class="hidden">
                <p id="upload-text" class="text-gray-600">Click to upload or drag & drop Excel file</p>
            </div>
            <div id="file-name-display" class="mt-4 hidden">
                <p class="text-sm text-gray-600">
                    <span id="processed-file-name"></span>
                </p>
            </div>
        </div>
        
        <div id="error-display" class="hidden bg-red-50 border border-red-200 rounded-lg p-4 mb-6">
            <h3 class="text-lg font-semibold text-red-800 mb-2">Errors & Warnings</h3>
            <div id="errors-list"></div>
        </div>
        
        <div id="data-preview" class="hidden">
            <div class="bg-white rounded-lg shadow-md p-6 mb-6">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-2xl font-bold">Data Preview</h2>
                    <button id="download-json-btn" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-lg transition">
                        Download JSON
                    </button>
                </div>
                
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                    <div class="bg-blue-50 p-4 rounded-lg">
                        <h3 class="text-sm font-semibold text-blue-800 mb-1">Categories</h3>
                        <p id="categories-count" class="text-2xl font-bold text-blue-900">0</p>
                    </div>
                    <div class="bg-green-50 p-4 rounded-lg">
                        <h3 class="text-sm font-semibold text-green-800 mb-1">Sample Queries</h3>
                        <p id="sample-queries-count" class="text-2xl font-bold text-green-900">0</p>
                    </div>
                    <div class="bg-purple-50 p-4 rounded-lg">
                        <h3 class="text-sm font-semibold text-purple-800 mb-1">Suggested Questions</h3>
                        <p id="suggested-questions-count" class="text-2xl font-bold text-purple-900">0</p>
                    </div>
                </div>
                
                <div id="categories-accordion" class="space-y-4"></div>
                
                <div class="mt-6">
                    <h3 class="text-lg font-semibold mb-3">Sample Queries</h3>
                    <div id="sample-queries-list" class="space-y-2"></div>
                </div>
                
                <div class="mt-6">
                    <h3 class="text-lg font-semibold mb-3">Suggested Questions</h3>
                    <div id="suggested-questions-list" class="space-y-2"></div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        class ExcelParser {
            parseFile(file) {
                return new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = (e) => {
                        try {
                            const data = new Uint8Array(e.target.result);
                            const workbook = XLSX.read(data, {type: 'array'});
                            const result = this.parseWorkbook(workbook);
                            resolve(result);
                        } catch (error) {
                            reject(error);
                        }
                    };
                    reader.onerror = reject;
                    reader.readAsArrayBuffer(file);
                });
            }
            
            parseWorkbook(workbook) {
                const errors = [];
                const categories = [];
                const sampleQueries = [];
                const suggestedQuestions = [];
                
                workbook.SheetNames.forEach(sheetName => {
                    const sheet = workbook.Sheets[sheetName];
                    const jsonData = XLSX.utils.sheet_to_json(sheet, {header: 1});
                    
                    if (sheetName.toLowerCase().includes('sample')) {
                        this.parseSampleQueries(jsonData, sampleQueries, errors);
                    } else if (sheetName.toLowerCase().includes('suggested')) {
                        this.parseSuggestedQuestions(jsonData, suggestedQuestions, errors);
                    } else {
                        this.parseCategory(sheetName, jsonData, categories, errors);
                    }
                });
                
                return {
                    data: {categories, sample_queries: sampleQueries, suggested_questions: suggestedQuestions},
                    errors
                };
            }
            
            parseCategory(categoryName, rows, categories, errors) {
                if (rows.length < 2) return;
                
                const tables = [];
                let currentTable = null;
                
                rows.forEach((row, idx) => {
                    if (!row || row.length === 0) return;
                    
                    const firstCell = String(row[0] || '').trim();
                    if (!firstCell) return;
                    
                    if (firstCell.toLowerCase() === 'table name') {
                        if (currentTable) tables.push(currentTable);
                        currentTable = {name: '', description: '', columns: []};
                    } else if (currentTable && !currentTable.name) {
                        currentTable.name = firstCell;
                        currentTable.description = String(row[1] || '').trim();
                    } else if (firstCell.toLowerCase() === 'column name') {
                        return;
                    } else if (currentTable && currentTable.name) {
                        currentTable.columns.push({
                            name: firstCell,
                            description: String(row[1] || '').trim(),
                            dataType: String(row[2] || '').trim()
                        });
                    }
                });
                
                if (currentTable) tables.push(currentTable);
                
                if (tables.length > 0) {
                    categories.push({name: categoryName, Tables: tables});
                }
            }
            
            parseSampleQueries(rows, sampleQueries, errors) {
                rows.slice(1).forEach(row => {
                    if (row && row[0]) {
                        sampleQueries.push({
                            user_input: String(row[0]).trim(),
                            description: String(row[1] || '').trim()
                        });
                    }
                });
            }
            
            parseSuggestedQuestions(rows, suggestedQuestions, errors) {
                rows.slice(1).forEach(row => {
                    if (row && row[0]) {
                        const questions = [];
                        for (let i = 1; i < row.length; i++) {
                            if (row[i]) questions.push(String(row[i]).trim());
                        }
                        suggestedQuestions.push({
                            user_input: String(row[0]).trim(),
                            suggested_questions: questions
                        });
                    }
                });
            }
            
            generateJSON(parsedData, selectedColumns) {
                const output = {
                    categories: parsedData.categories.map(cat => ({
                        name: cat.name,
                        Tables: cat.Tables.map(table => ({
                            name: table.name,
                            description: table.description,
                            columns: table.columns.filter(col => {
                                const colId = `${cat.name}-${table.name}-${col.name}`;
                                return selectedColumns.get(colId);
                            }).map(col => ({
                                name: col.name,
                                description: col.description,
                                dataType: col.dataType
                            }))
                        }))
                    })),
                    sample_queries: parsedData.sample_queries,
                    suggested_questions: parsedData.suggested_questions
                };
                return output;
            }
        }
        
        let parsedData = null;
        let selectedColumns = new Map();
        let isProcessing = false;
        
        const fileInput = document.getElementById('file-input');
        const fileUploadArea = document.getElementById('file-upload-area');
        const uploadText = document.getElementById('upload-text');
        const fileNameDisplay = document.getElementById('file-name-display');
        const processedFileName = document.getElementById('processed-file-name');
        const errorDisplay = document.getElementById('error-display');
        const errorsList = document.getElementById('errors-list');
        const dataPreview = document.getElementById('data-preview');
        const downloadBtn = document.getElementById('download-json-btn');
        const categoriesCount = document.getElementById('categories-count');
        const sampleQueriesCount = document.getElementById('sample-queries-count');
        const suggestedQuestionsCount = document.getElementById('suggested-questions-count');
        const categoriesAccordion = document.getElementById('categories-accordion');
        const sampleQueriesList = document.getElementById('sample-queries-list');
        const suggestedQuestionsList = document.getElementById('suggested-questions-list');
        
        function showToast(message, type = 'success') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `toast toast-${type}`;
            toast.textContent = message;
            container.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }
        
        async function handleFileSelect(file) {
            isProcessing = true;
            uploadText.textContent = 'Processing...';
            fileUploadArea.classList.add('opacity-50', 'pointer-events-none');
            
            try {
                const parser = new ExcelParser();
                const result = await parser.parseFile(file);
                parsedData = result.data;
                
                if (parsedData && parsedData.categories) {
                    parsedData.categories = parsedData.categories.map(category => ({
                        ...category,
                        Tables: category.Tables.map(table => ({
                            ...table,
                            columns: table.columns.map(column => {
                                const columnId = `${category.name}-${table.name}-${column.name}`;
                                selectedColumns.set(columnId, true);
                                return {...column, id: columnId};
                            })
                        }))
                    }));
                }
                
                showToast('File parsed successfully!', 'success');
                processedFileName.textContent = `Processed: ${file.name}`;
                fileNameDisplay.classList.remove('hidden');
                renderDataPreview();
                
            } catch (error) {
                showToast('Failed to parse file', 'error');
            } finally {
                isProcessing = false;
                uploadText.textContent = 'Upload Excel File';
                fileUploadArea.classList.remove('opacity-50', 'pointer-events-none');
            }
        }
        
        function renderDataPreview() {
            if (!parsedData) return;
            
            dataPreview.classList.remove('hidden');
            categoriesCount.textContent = parsedData.categories.length;
            sampleQueriesCount.textContent = parsedData.sample_queries.length;
            suggestedQuestionsCount.textContent = parsedData.suggested_questions.length;
            
            categoriesAccordion.innerHTML = '';
            parsedData.categories.forEach((category, catIdx) => {
                const categoryDiv = document.createElement('div');
                categoryDiv.className = 'border rounded-lg p-4 bg-gray-50';
                categoryDiv.innerHTML = `
                    <h3 class="text-lg font-semibold mb-3">${category.name}</h3>
                    ${category.Tables.map(table => `
                        <div class="mb-4 bg-white p-3 rounded">
                            <h4 class="font-medium mb-2">${table.name}</h4>
                            <p class="text-sm text-gray-600 mb-2">${table.description}</p>
                            <div class="grid grid-cols-2 gap-2">
                                ${table.columns.map(col => `
                                    <label class="flex items-center space-x-2">
                                        <input type="checkbox" class="rounded" data-column-id="${col.id}" ${selectedColumns.get(col.id) ? 'checked' : ''}>
                                        <span class="text-sm">${col.name}</span>
                                    </label>
                                `).join('')}
                            </div>
                        </div>
                    `).join('')}
                `;
                categoriesAccordion.appendChild(categoryDiv);
            });
            
            sampleQueriesList.innerHTML = '';
            parsedData.sample_queries.forEach(query => {
                const div = document.createElement('div');
                div.className = 'border rounded p-3 bg-gray-50';
                div.innerHTML = `<p class="font-medium">${query.user_input}</p>`;
                sampleQueriesList.appendChild(div);
            });
            
            suggestedQuestionsList.innerHTML = '';
            parsedData.suggested_questions.forEach(item => {
                const div = document.createElement('div');
                div.className = 'border rounded p-3 bg-gray-50';
                div.innerHTML = `<p class="font-medium mb-2">${item.user_input}</p>`;
                suggestedQuestionsList.appendChild(div);
            });
            
            document.querySelectorAll('input[type="checkbox"][data-column-id]').forEach(checkbox => {
                checkbox.addEventListener('change', (e) => {
                    selectedColumns.set(e.target.dataset.columnId, e.target.checked);
                });
            });
        }
        
        function handleDownload() {
            if (!parsedData) return;
            const parser = new ExcelParser();
            const json = parser.generateJSON(parsedData, selectedColumns);
            const blob = new Blob([JSON.stringify(json, null, 2)], {type: 'application/json'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'metadata_output.json';
            a.click();
            URL.revokeObjectURL(url);
            showToast('JSON downloaded!', 'success');
        }
        
        fileUploadArea.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', (e) => {
            if (e.target.files[0]) handleFileSelect(e.target.files[0]);
        });
        fileUploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            const file = Array.from(e.dataTransfer.files).find(f => f.name.match(/\.xlsx?$/i));
            if (file) handleFileSelect(file);
        });
        fileUploadArea.addEventListener('dragover', (e) => e.preventDefault());
        downloadBtn.addEventListener('click', handleDownload);
    </script>
</body>
</html>
    """
    
    components.html(html_content, height=1200, scrolling=True)

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
        ["📝 Prompt Editor", "🔍 Environment Comparison", "📊 Excel to JSON Converter"]
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

if __name__ == "__main__":
    main()
