import os
import sys
import hashlib
import socket
import platform
import argparse
from datetime import datetime, timezone
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
import io

# 权限和 MIME 映射保持不变
SCOPES = ['https://www.googleapis.com/auth/drive']
MIME_TYPE_MAP = {
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'application/vnd.google-apps.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'application/vnd.google-apps.spreadsheet',
    'text/csv': 'application/vnd.google-apps.spreadsheet',
    'text/plain': 'application/vnd.google-apps.document',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'application/vnd.google-apps.presentation',
}

# --- 辅助函数 get_drive_service, calculate_md5, get_or_create_folder_id, get_remote_path_id, get_default_config_dir 保持不变 ---
def get_drive_service(credentials_path, token_path):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_path):
                print(f"错误: 凭证文件未找到 -> '{credentials_path}'")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token_file:
            token_file.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)

def calculate_md5(file_path):
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def get_or_create_folder_id(service, folder_name, parent_id='root'):
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed = false"
    response = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
    files = response.get('files', [])
    if files: return files[0].get('id')
    folder_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    folder = service.files().create(body=folder_metadata, fields='id').execute()
    print(f"  - 在云端创建了新文件夹: '{folder_name}'")
    return folder.get('id')

def get_remote_path_id(service, remote_path_parts):
    current_parent_id = 'root'
    for part in remote_path_parts:
        if part:
            current_parent_id = get_or_create_folder_id(service, part, current_parent_id)
    return current_parent_id

def get_default_config_dir():
    if platform.system() == "Darwin":
        return os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/AppConfig/drive-sync')
    else:
        return os.path.expanduser('~/.config/drive-sync')

# *** 主要改动点: find_remote_file 函数变得更智能 ***
def find_remote_file(service, file_name, parent_folder_id):
    """在指定父文件夹中智能查找文件，兼容文件名被转换的情况"""
    base_name, extension = os.path.splitext(file_name)
    
    # 如果文件本身没有扩展名，或基础名为空，则只按全名搜索
    if not extension or not base_name:
        query = f"name = '{file_name}' and '{parent_folder_id}' in parents and trashed = false"
    else:
        # 否则，同时搜索 "file.ext" 和 "file"
        # 使用 ' or ' 来组合查询条件
        query = f"(name = '{file_name}' or name = '{base_name}') and '{parent_folder_id}' in parents and trashed = false"
    
    response = service.files().list(
        q=query,
        spaces='drive',
        # 多请求一个 mimeType 字段用于判断
        fields='files(id, name, md5Checksum, modifiedTime, webViewLink, mimeType)',
        pageSize=10 # 请求多个以便后续筛选
    ).execute()
    files = response.get('files', [])
    
    if not files:
        return None
    
    # 筛选逻辑：如果找到了多个文件（例如 a.xlsx 和 a），优先返回那个看起来被转换过的原生文件
    if len(files) > 1:
        for f in files:
            if 'google-apps' in f.get('mimeType', ''):
                return f # 优先返回原生Google文档
    
    return files[0] # 如果没找到原生文档或只有一个结果，返回第一个


def main():
    """主执行函数"""
    parser = argparse.ArgumentParser(description="智能同步本地文件至 Google Drive", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("local_path", help="要同步的本地文件路径")
    parser.add_argument("--base-path", default="/FileSync", help="云端存储的基础路径")
    parser.add_argument("--sync-direction", choices=['auto', 'local-to-remote', 'remote-to-local'], default='auto', help="同步方向")
    parser.add_argument("--credentials-path", help="指定 credentials.json 文件的路径")
    parser.add_argument("--token-path", help="指定 token.json 文件的路径")
    args = parser.parse_args()

    default_config_dir = get_default_config_dir()
    os.makedirs(default_config_dir, exist_ok=True)
    credentials_path = args.credentials_path or os.path.join(default_config_dir, 'credentials.json')
    token_path = args.token_path or os.path.join(default_config_dir, 'token.json')
    print(f"凭证文件 (Credentials) 路径: {credentials_path}")
    print(f"令牌文件 (Token) 路径: {token_path}")

    local_file_path = os.path.abspath(args.local_path)
    if not os.path.isfile(local_file_path):
        print(f"错误: 提供的路径不是一个有效的文件 -> '{local_file_path}'")
        return

    device_name = socket.gethostname()
    path_without_drive = os.path.splitdrive(local_file_path)[1]
    path_parts = path_without_drive.strip(os.path.sep).split(os.path.sep)
    if platform.system() == "Windows":
        drive = os.path.splitdrive(local_file_path)[0].strip(":\\")
        path_parts.insert(0, drive)
    base_path_parts = args.base_path.strip('/').split('/')
    remote_folder_path_parts = base_path_parts + [device_name] + path_parts[:-1]
    file_name = path_parts[-1]
    print(f"\n本地文件: {local_file_path}")
    print(f"将同步至云端路径: {'/'.join(remote_folder_path_parts)}/{file_name}")

    try:
        service = get_drive_service(credentials_path, token_path)
        print("\n正在检查并创建云端文件夹结构...")
        parent_folder_id = get_remote_path_id(service, remote_folder_path_parts)
        print(f"\n正在云端智能搜索文件 '{file_name}' (或 '{os.path.splitext(file_name)[0]}')...")
        remote_file = find_remote_file(service, file_name, parent_folder_id)

        if not remote_file:
            print("远程文件不存在，执行上传操作。")
            media = MediaFileUpload(local_file_path, resumable=True)
            file_metadata = {'name': file_name, 'parents': [parent_folder_id]}
            if media.mimetype() in MIME_TYPE_MAP:
                file_metadata['mimeType'] = MIME_TYPE_MAP[media.mimetype()]
            uploaded_file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
            print(f"\n✅ 上传成功！\n   编辑链接: {uploaded_file.get('webViewLink')}")
            return

        print(f"远程文件已找到: '{remote_file.get('name')}', 开始进行比较...")
        
        # *** 主要改动点: 智能同步逻辑 ***
        is_native_google_doc = 'md5Checksum' not in remote_file
        
        if is_native_google_doc:
            print("  - 检测到云端文件为原生Google格式，将跳过MD5比对。")
        else:
            local_md5 = calculate_md5(local_file_path)
            remote_md5 = remote_file.get('md5Checksum')
            print(f"  - 本地文件 MD5: {local_md5}\n  - 云端文件 MD5: {remote_md5}")
            if local_md5 == remote_md5:
                print("\n✅ 文件内容完全一致，无需同步。")
                print(f"   编辑链接: {remote_file.get('webViewLink')}")
                return

        print("正在比较修改时间...")
        local_mtime_utc = datetime.fromtimestamp(os.path.getmtime(local_file_path), tz=timezone.utc)
        remote_mtime_utc = datetime.fromisoformat(remote_file.get('modifiedTime').replace('Z', '+00:00'))
        print(f"  - 本地文件修改时间 (UTC): {local_mtime_utc}\n  - 云端文件修改时间 (UTC): {remote_mtime_utc}")
        
        effective_direction = args.sync_direction
        if effective_direction == 'auto':
            if local_mtime_utc > remote_mtime_utc:
                effective_direction = 'local-to-remote'
                print("\n自动检测：本地文件较新。")
            else:
                effective_direction = 'remote-to-local'
                print("\n自动检测：云端文件较新或时间相同。")

        if effective_direction == 'local-to-remote' and (is_native_google_doc or local_mtime_utc > remote_mtime_utc):
            if not is_native_google_doc and local_mtime_utc <= remote_mtime_utc:
                print(f"\n⏭️ 本地文件不是最新的，根据 '{args.sync_direction}' 规则，跳过操作。")
            else:
                print("执行上传覆盖云端文件...")
                media = MediaFileUpload(local_file_path, resumable=True)
                updated_file = service.files().update(fileId=remote_file.get('id'), media_body=media, fields='id, webViewLink').execute()
                print(f"✅ 更新成功！\n   编辑链接: {updated_file.get('webViewLink')}")
        elif effective_direction == 'remote-to-local' and (is_native_google_doc or remote_mtime_utc > local_mtime_utc):
             if not is_native_google_doc and remote_mtime_utc <= local_mtime_utc:
                print(f"\n⏭️ 云端文件不是最新的，根据 '{args.sync_direction}' 规则，跳过操作。")
             else:
                print("执行下载覆盖本地文件...")
                request = service.files().get_media(fileId=remote_file.get('id'))
                with io.FileIO(local_file_path, 'wb') as fh:
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: status, done = downloader.next_chunk()
                print(f"\n✅ 下载成功！本地文件已被更新。")
        
        print(f"   云端文件链接: {remote_file.get('webViewLink')}")

    except HttpError as error:
        print(f"发生 API 错误: {error}")
    except FileNotFoundError:
        print(f"错误: 本地文件未找到 -> '{local_file_path}'")
    except Exception as e:
        print(f"发生未知错误: {e}")

if __name__ == '__main__':
    main()