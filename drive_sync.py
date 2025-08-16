import os
import sys
import hashlib
import socket
import platform
import argparse
import webbrowser
from datetime import datetime, timezone
import io

# 導入 rich 相關的模組
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

# 導入 Google API 相關模組
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

# --- 常數定義 ---
SCOPES = ['https://www.googleapis.com/auth/drive']
MIME_TYPE_MAP = {
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'application/vnd.google-apps.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'application/vnd.google-apps.spreadsheet',
    'text/csv': 'application/vnd.google-apps.spreadsheet',
    'text/plain': 'application/vnd.google-apps.document',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'application/vnd.google-apps.presentation',
}
GOOGLE_DOC_EXPORT_MAP = {
    'application/vnd.google-apps.document': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.google-apps.spreadsheet': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.google-apps.presentation': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}

# --- 輔助函數 ---

def get_drive_service(credentials_path, token_path, console):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        with console.status("[bold yellow]需要授權，請在瀏覽器中操作...", spinner="dots"):
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(credentials_path):
                    console.print(f"[bold red]錯誤:[/bold red] 憑證文件未找到 -> '{credentials_path}'")
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

def get_or_create_folder_id(service, folder_name, console, parent_id='root'):
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed = false"
    response = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
    files = response.get('files', [])
    if files: return files[0].get('id')
    folder_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    folder = service.files().create(body=folder_metadata, fields='id').execute()
    console.log(f"  - 在雲端創建了新資料夾: '[bold cyan]{folder_name}[/bold cyan]'")
    return folder.get('id')

def get_remote_path_id(service, remote_path_parts, console):
    current_parent_id = 'root'
    for part in remote_path_parts:
        if part:
            current_parent_id = get_or_create_folder_id(service, part, console, current_parent_id)
    return current_parent_id

def find_remote_file(service, file_name, parent_folder_id):
    base_name, _ = os.path.splitext(file_name)
    query = f"(name = '{file_name}' or name = '{base_name}') and '{parent_folder_id}' in parents and trashed = false"
    response = service.files().list(q=query, spaces='drive', fields='files(id, name, md5Checksum, modifiedTime, webViewLink, mimeType)', pageSize=10).execute()
    files = response.get('files', [])
    if not files: return None
    if len(files) > 1:
        for f in files:
            if 'google-apps' in f.get('mimeType', ''): return f
    return files[0]

def get_default_config_dir():
    if platform.system() == "Darwin": return os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/AppConfig/drive-sync')
    else: return os.path.expanduser('~/.config/drive-sync')

def create_drive_file(service, local_file_path, file_name, parent_folder_id, console):
    media = MediaFileUpload(local_file_path, resumable=True)
    file_metadata = {'name': file_name, 'parents': [parent_folder_id]}
    if media.mimetype() in MIME_TYPE_MAP:
        file_metadata['mimeType'] = MIME_TYPE_MAP[media.mimetype()]
        console.log(f"  - 請求將文件轉換為: [bold yellow]{file_metadata['mimeType']}[/bold yellow]")
    uploaded_file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
    return uploaded_file

def open_in_browser(url, console):
    if not url:
        console.print("  - [bold red]警告:[/bold red] 未提供有效的URL，無法打開瀏覽器。")
        return
    console.log(f"  - 正在嘗試在預設瀏覽器中打開連結...")
    try:
        webbrowser.open(url, new=2)
    except Exception as e:
        console.print(f"  - [bold red]錯誤:[/bold red] 自動打開瀏覽器失敗: {e}")

# --- 主執行函數 ---

def main():
    console = Console()

    parser = argparse.ArgumentParser(description="智能同步本地文件至 Google Drive", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("local_path", help="要同步的本地文件路徑")
    parser.add_argument("--base-path", default="/FileSync", help="雲端儲存的基礎路徑")
    parser.add_argument("--sync-direction", choices=['auto', 'local-to-remote', 'remote-to-local'], default='auto', help="同步方向")
    parser.add_argument("--credentials-path", help="指定 credentials.json 文件的路徑")
    parser.add_argument("--token-path", help="指定 token.json 文件的路徑")
    parser.add_argument("--open", action='store_true', help="同步完成後，在預設瀏覽器中自動打開文件連結")
    args = parser.parse_args()

    local_file_path = os.path.abspath(args.local_path)
    if not os.path.isfile(local_file_path):
        console.print(f"[bold red]錯誤:[/bold red] 提供的路徑不是一個有效的文件 -> '{local_file_path}'")
        return

    default_config_dir = get_default_config_dir()
    os.makedirs(default_config_dir, exist_ok=True)
    credentials_path = args.credentials_path or os.path.join(default_config_dir, 'credentials.json')
    token_path = args.token_path or os.path.join(default_config_dir, 'token.json')
    
    device_name = socket.gethostname()
    path_without_drive = os.path.splitdrive(local_file_path)[1]
    path_parts = path_without_drive.strip(os.path.sep).split(os.path.sep)
    if platform.system() == "Windows":
        drive = os.path.splitdrive(local_file_path)[0].strip(":\\")
        path_parts.insert(0, drive)
    base_path_parts = args.base_path.strip('/').split('/')
    remote_folder_path_parts = base_path_parts + [device_name] + path_parts[:-1]
    file_name = path_parts[-1]
    
    info_table = Table(show_header=False, box=None, padding=(0, 1))
    info_table.add_row("[bold]本地文件:[/bold]", f"[green]{local_file_path}[/green]")
    info_table.add_row("[bold]雲端路徑:[/bold]", f"[cyan]/{'/'.join(remote_folder_path_parts)}/{file_name}[/cyan]")
    console.print(Panel(info_table, title="同步任務", border_style="blue", expand=False))
    
    try:
        service = get_drive_service(credentials_path, token_path, console)
        
        with console.status("[bold green]正在初始化...", spinner="dots") as status:
            status.update("[bold green]正在檢查並創建雲端資料夾結構...")
            parent_folder_id = get_remote_path_id(service, remote_folder_path_parts, console)
            status.update(f"[bold green]正在雲端智能搜索文件 '[yellow]{file_name}[/yellow]'...")
            remote_file = find_remote_file(service, file_name, parent_folder_id)

        if not remote_file:
            console.print("\n[yellow]遠端文件不存在，執行上傳操作。[/yellow]")
            with console.status("[bold green]文件上傳中...", spinner="earth"):
                uploaded_file = create_drive_file(service, local_file_path, file_name, parent_folder_id, console)
            
            file_link = uploaded_file.get('webViewLink')
            summary = Text.assemble(("上傳成功！\n", "bold green"), ("編輯連結: ", "default"), (file_link, "cyan underline"))
            console.print(Panel(summary, title="✅ 操作完成", border_style="green"))
            if args.open: open_in_browser(file_link, console)
            return

        console.print(f"\n[green]遠端文件已找到: '[bold]{remote_file.get('name')}[/bold]', 開始進行比較...[/green]")
        is_native_google_doc = 'google-apps' in remote_file.get('mimeType', '')
        remote_file_link = remote_file.get('webViewLink')

        if is_native_google_doc:
            console.print("  - [magenta]檢測到雲端文件為原生Google格式，將跳過MD5比對。[/magenta]")
            proceed_to_time_comparison = True
        else:
            with console.status("[bold green]正在計算本地文件 MD5..."):
                local_md5 = calculate_md5(local_file_path)
            remote_md5 = remote_file.get('md5Checksum')
            console.print(f"  - 本地文件 MD5: [yellow]{local_md5}[/yellow]\n  - 雲端文件 MD5: [yellow]{remote_md5}[/yellow]")
            if local_md5 == remote_md5:
                summary = Text.assemble(("文件內容完全一致，無需同步。\n", "bold green"), ("編輯連結: ", "default"), (remote_file_link, "cyan underline"))
                console.print(Panel(summary, title="✅ 操作完成", border_style="green"))
                if args.open: open_in_browser(remote_file_link, console)
                return
            else:
                proceed_to_time_comparison = True

        if proceed_to_time_comparison:
            console.print("\n[bold]正在比較修改時間...[/bold]")
            local_mtime_utc = datetime.fromtimestamp(os.path.getmtime(local_file_path), tz=timezone.utc)
            remote_mtime_utc = datetime.fromisoformat(remote_file.get('modifiedTime').replace('Z', '+00:00'))
            console.print(f"  - 本地文件 (UTC): [yellow]{local_mtime_utc}[/yellow]\n  - 雲端文件 (UTC): [yellow]{remote_mtime_utc}[/yellow]")

            effective_direction = args.sync_direction
            if effective_direction == 'auto':
                if local_mtime_utc > remote_mtime_utc:
                    effective_direction = 'local-to-remote'
                    console.print("\n[bold blue]自動檢測:[/bold blue] 本地文件較新。")
                else:
                    effective_direction = 'remote-to-local'
                    console.print("\n[bold blue]自動檢測:[/bold blue] 雲端文件較新或時間相同。")

            if effective_direction == 'local-to-remote' and local_mtime_utc > remote_mtime_utc:
                if is_native_google_doc:
                    with console.status("[bold green]正在更新原生Google文件(刪除後重建)...", spinner="bouncingBar") as status:
                        status.update("[bold red]  - 正在刪除舊文件...")
                        service.files().delete(fileId=remote_file.get('id')).execute()
                        status.update("[bold green]  - 正在上傳新版本...")
                        updated_file = create_drive_file(service, local_file_path, file_name, parent_folder_id, console)
                    
                    file_link = updated_file.get('webViewLink')
                    summary = Text.assemble(("更新成功！文件已被重建。\n", "bold green"), ("新的編輯連結: ", "default"), (file_link, "cyan underline"))
                    console.print(Panel(summary, title="✅ 操作完成", border_style="green"))
                    if args.open: open_in_browser(file_link, console)
                else:
                    with console.status("[bold green]文件更新中...", spinner="earth"):
                        media = MediaFileUpload(local_file_path, resumable=True)
                        updated_file = service.files().update(fileId=remote_file.get('id'), media_body=media, fields='id, webViewLink').execute()
                    
                    file_link = updated_file.get('webViewLink')
                    summary = Text.assemble(("更新成功！\n", "bold green"), ("編輯連結: ", "default"), (file_link, "cyan underline"))
                    console.print(Panel(summary, title="✅ 操作完成", border_style="green"))
                    if args.open: open_in_browser(file_link, console)

            elif effective_direction == 'remote-to-local' and remote_mtime_utc > local_mtime_utc:
                with console.status("[bold green]正在從雲端下載文件...", spinner="arrow3") as status:
                    if is_native_google_doc:
                        remote_mime_type = remote_file.get('mimeType')
                        export_mime_type = GOOGLE_DOC_EXPORT_MAP.get(remote_mime_type)
                        if not export_mime_type:
                            console.print(f"\n[bold red]❌ 下載失敗！[/bold red] 不支持將 '{remote_mime_type}' 導出為此類文件。")
                            return
                        status.update(f"[bold green]檢測到原生Google格式，將從 '{remote_mime_type}' 導出為 '{export_mime_type}'...")
                        request = service.files().export_media(fileId=remote_file.get('id'), mimeType=export_mime_type)
                    else:
                        request = service.files().get_media(fileId=remote_file.get('id'))
                    
                    with io.FileIO(local_file_path, 'wb') as fh:
                        downloader = MediaIoBaseDownload(fh, request)
                        done = False
                        while not done: 
                            # 使用一个不会冲突的新变量名 download_progress
                            download_progress, done = downloader.next_chunk()
                            # 使用 download_progress 获取进度，并用原始的 status 对象来更新显示
                            if download_progress:
                                status.update(f"[bold green]下載進度: {int(download_progress.progress() * 100)}%")
                
                summary = Text.assemble(("下載成功！本地文件已被更新。\n", "bold green"), ("雲端文件連結: ", "default"), (remote_file_link, "cyan underline"))
                console.print(Panel(summary, title="✅ 操作完成", border_style="green"))
                if args.open: open_in_browser(remote_file_link, console)
            else:
                summary = Text.assemble((f"文件不是最新的，根據 '{args.sync_direction}' 規則，跳過操作。\n", "bold yellow"), ("雲端文件連結: ", "default"), (remote_file_link, "cyan underline"))
                console.print(Panel(summary, title="⏭️ 操作跳過", border_style="yellow"))
                if args.open: open_in_browser(remote_file_link, console)

    except HttpError as error:
        console.print(Panel(f"[bold]API 錯誤詳情:[/bold]\n{error}", title="❌ API 錯誤", border_style="bold red"))
    except FileNotFoundError:
        console.print(Panel(f"本地文件未找到 -> '{local_file_path}'", title="❌ 文件錯誤", border_style="bold red"))
    except Exception as e:
        console.print(Panel(f"[bold]未知錯誤詳情:[/bold]\n{e}", title="❌ 未知錯誤", border_style="bold red"))

if __name__ == '__main__':
    main()