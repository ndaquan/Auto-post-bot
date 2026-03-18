import gspread
from google.oauth2.service_account import Credentials
import requests
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from dateutil import parser
import os
import time
import logging
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
 
# === LOGGING ===
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("fb_video_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
 
def log(message):
    logging.info(message)
 
# Load biến từ file .env
load_dotenv()
 
# === CONFIG từ .env ===
APP_ID = os.getenv("APP_ID")
USER_ACCESS_TOKEN = os.getenv("USER_ACCESS_TOKEN")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
 
SHEET_NAME = os.getenv("SHEET_NAME", "AutoPost Video Bot - Content Queue")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Video Queue")
 
# Pages dict từ .env
PAGES = {
    "Page LunaVy": {
        "page_id": os.getenv("PAGE1_ID"),
        "page_token": os.getenv("PAGE1_TOKEN")
    },
    "Page Banh Ngot": {
        "page_id": os.getenv("PAGE2_ID"),
        "page_token": os.getenv("PAGE2_TOKEN")
    }
}
 
# Kiểm tra biến bắt buộc
required_vars = ["APP_ID", "USER_ACCESS_TOKEN", "SERVICE_ACCOUNT_FILE", "PAGE1_ID", "PAGE1_TOKEN"]
for var in required_vars:
    if not os.getenv(var):
        raise ValueError(f"Thiếu biến môi trường bắt buộc: {var}. Kiểm tra file .env")
 
# Kết nối Google Sheet
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scope)
# FIX: dùng gspread.authorize() thay vì gspread.Client(auth=creds)
# gspread.Client() không khởi tạo AuthorizedSession → SSL bị treo khi gọi API đầu tiên
client = gspread.authorize(creds)
 
def get_sheet():
    """Lazy-connect sheet, tự reconnect nếu token hết hạn."""
    return client.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
 
try:
    sheet = get_sheet()
    log("Kết nối Google Sheet thành công.")
except Exception as e:
    log(f"LỖI kết nối Google Sheet lúc khởi động: {e}")
    raise
 
def convert_gdrive_url(url):
    """Chuyển link Google Drive sang direct download link."""
    if "drive.google.com/file/d/" in url:
        file_id = url.split("/file/d/")[1].split("/")[0]
        return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    return url
 
def upload_video_to_fb(page_id, page_token, video_source, title, desc, thumb_url=None, scheduled_timestamp=None):
    """
    Upload video lên Facebook Page.
    - Nếu video_source là URL: dùng file_url API
    - Nếu là đường dẫn local: dùng resumable upload
    - scheduled_timestamp: Unix timestamp (chỉ dùng nếu > now + 10 phút, theo yêu cầu FB)
    """
    log("  Bắt đầu upload video...")
 
    # Xác định có schedule hay không (FB yêu cầu scheduled_time > now + 10 phút)
    use_schedule = False
    if scheduled_timestamp:
        min_schedule_ts = int(time.time()) + 10 * 60  # 10 phút từ bây giờ
        if scheduled_timestamp > min_schedule_ts:
            use_schedule = True
        else:
            log("  - Thời gian schedule đã qua hoặc quá gần → đăng ngay (published=true)")
 
    # --- Upload qua URL ---
    if video_source.startswith("http"):
        data = {
            "access_token": page_token,
            "title": title,
            "description": desc,
            "file_url": video_source,
        }
        if use_schedule:
            data["scheduled_publish_time"] = scheduled_timestamp
            data["published"] = "false"
        else:
            data["published"] = "true"
 
        if thumb_url:
            data["thumb"] = thumb_url
 
        try:
            response = requests.post(
                f"https://graph-video.facebook.com/v25.0/{page_id}/videos",
                data=data
            ).json()
            if "id" in response:
                log(f"  → Đăng thành công (file_url)! Post ID: {response['id']}")
                return response["id"]
            else:
                log(f"  → Lỗi file_url: {response}")
                return None
        except Exception as e:
            log(f"  → Exception file_url: {e}")
            return None
 
    # --- Upload file local (resumable) ---
    else:
        if not os.path.exists(video_source):
            log(f"  → File không tồn tại: {video_source}")
            return None
 
        file_size = os.path.getsize(video_source)
        log(f"  - Upload resumable từ local: {video_source} ({file_size} bytes)")
 
        # Bước 1: Khởi tạo session upload
        init_url = f"https://graph-video.facebook.com/v25.0/{page_id}/videos"
        init_data = {
            "access_token": page_token,
            "upload_phase": "start",
            "file_size": file_size,
        }
        if use_schedule:
            init_data["scheduled_publish_time"] = scheduled_timestamp
            init_data["published"] = "false"
 
        init_response = requests.post(init_url, data=init_data).json()
        if "upload_session_id" not in init_response:
            log(f"  → Lỗi khởi tạo session: {init_response}")
            return None
 
        upload_session_id = init_response["upload_session_id"]
        start_offset = int(init_response.get("start_offset", 0))
        log(f"  - Session ID: {upload_session_id}, Start offset: {start_offset}")
 
        # Bước 2: Upload chunk với retry
        upload_url = f"https://rupload.facebook.com/video-upload/v25.0/{upload_session_id}"
        CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
        MAX_RETRIES = 3
 
        with open(video_source, "rb") as f:
            f.seek(start_offset)
            chunk = f.read(CHUNK_SIZE)
            while chunk:
                headers = {
                    "Authorization": f"OAuth {page_token}",
                    "offset": str(start_offset),
                    "file_size": str(file_size),
                }
                # Retry logic
                success_chunk = False
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        response = requests.post(upload_url, headers=headers, data=chunk, timeout=60)
                        if response.status_code == 200:
                            resp_json = response.json()
                            start_offset = int(resp_json.get("start_offset", start_offset + len(chunk)))
                            success_chunk = True
                            break
                        else:
                            log(f"  → Lỗi chunk (attempt {attempt}/{MAX_RETRIES}): {response.text}")
                            time.sleep(2 ** attempt)  # exponential backoff: 2s, 4s, 8s
                    except Exception as e:
                        log(f"  → Exception chunk (attempt {attempt}/{MAX_RETRIES}): {e}")
                        time.sleep(2 ** attempt)
 
                if not success_chunk:
                    log("  → Upload chunk thất bại sau tất cả retry. Hủy upload.")
                    return None
 
                chunk = f.read(CHUNK_SIZE)
 
        # Bước 3: Finish upload
        finish_url = f"https://graph-video.facebook.com/v25.0/{page_id}/videos"
        finish_data = {
            "access_token": page_token,
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "title": title,
            "description": desc,
        }
        if thumb_url:
            finish_data["thumb"] = thumb_url
 
        finish_response = requests.post(finish_url, data=finish_data).json()
        if "id" in finish_response:
            log(f"  → Resumable upload thành công! Post ID: {finish_response['id']}")
            return finish_response["id"]
        else:
            log(f"  → Lỗi finish: {finish_response}")
            return None
 
 
def get_column_indices():
    """Cache tất cả column index cần dùng — chỉ gọi 1 lần mỗi lượt quét."""
    header_row = sheet.row_values(1)
    col_map = {name: idx + 1 for idx, name in enumerate(header_row)}
    required_cols = ["Status", "Post ID (Result)"]
    for col in required_cols:
        if col not in col_map:
            raise ValueError(f"Không tìm thấy cột '{col}' trong sheet. Kiểm tra tên cột.")
    return col_map
 
 
def process_sheet():
    global sheet
    try:
        sheet = get_sheet()  # Refresh kết nối mỗi lần chạy, tránh token hết hạn
        records = sheet.get_all_records()
        log(f"Bắt đầu quét sheet | Tổng số row: {len(records)}")
    except Exception as e:
        log(f"LỖI đọc sheet: {e}")
        return
 
    # FIX: Cache column index một lần trước vòng lặp, tránh gọi API mỗi row
    try:
        col_map = get_column_indices()
    except Exception as e:
        log(f"LỖI lấy column index: {e}")
        return
 
    col_status = col_map["Status"]
    col_post_id = col_map["Post ID (Result)"]
 
    vn_tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now_vn = datetime.now(vn_tz)
    today_str = now_vn.strftime("%d/%m/%Y")
 
    log(f"  - Hôm nay (VN): {today_str} | Giờ hiện tại: {now_vn.strftime('%H:%M:%S')}")
 
    processed_count = 0
    skipped_count = 0
 
    for row_idx, row in enumerate(records, start=2):
        scheduled_date = str(row.get("Scheduled Date", "")).strip()
        scheduled_slot = str(row.get("Scheduled Time", "")).strip()
        status = str(row.get("Status", "")).strip().lower()
        page_choice = row.get("Page", "").strip()
        video_source = row.get("Video Source", "").strip()
        video_source = convert_gdrive_url(video_source)
 
        # 1. Kiểm tra ngày hôm nay
        if scheduled_date != today_str:
            skipped_count += 1
            continue
 
        # 2. Kiểm tra status — bỏ qua Processing để tránh double-post
        if status not in ["ready", "pending"]:
            skipped_count += 1
            continue
 
        # 3. Kiểm tra slot hợp lệ
        scheduled_slot = str(row.get("Scheduled Time", "")).strip()[:5]
        if scheduled_slot not in ["13:00", "21:00"]:
            log(f"  Row {row_idx} → Skip: Slot không hợp lệ ({scheduled_slot})")
            skipped_count += 1
            continue
 
        # 4. Tính thời gian đăng
        try:
            slot_hour = int(scheduled_slot.split(":")[0])
            scheduled_dt_str = f"{scheduled_date} {slot_hour}:00"
            scheduled_dt = parser.parse(scheduled_dt_str, dayfirst=True)
            scheduled_dt = scheduled_dt.replace(tzinfo=vn_tz)
            unix_ts = int(scheduled_dt.timestamp())
        except Exception as e:
            log(f"  Row {row_idx} → Lỗi parse ngày/giờ: {e}")
            skipped_count += 1
            continue
 
        # 5. Kiểm tra đã tới giờ chưa
        if now_vn < scheduled_dt:
            skipped_count += 1
            continue
 
        log(f"  Row {row_idx}: [{page_choice}] {scheduled_date} {scheduled_slot} | {video_source[:60]}...")
 
        # FIX: Lock row ngay lập tức trước khi upload — tránh double-post khi interval chạy lại
        try:
            sheet.update_cell(row_idx, col_status, "Processing")
        except Exception as e:
            log(f"  Row {row_idx} → Lỗi lock row: {e}")
            skipped_count += 1
            continue
 
        processed_count += 1
 
        title = row.get("Generated Title") or row.get("Video Title (Manual)", "No Title")
        desc = row.get("Generated Description") or row.get("Video Description (Manual)", "No Description")
        thumb = row.get("Thumbnail URL / Path", "").strip()
 
        pages_to_post = []
        if page_choice == "Both":
            pages_to_post = ["Page1", "Page2"]
        elif page_choice in PAGES:
            pages_to_post = [page_choice]
        else:
            log(f"  Row {row_idx} → Skip: Page không hợp lệ ({page_choice})")
            sheet.update_cell(row_idx, col_status, "Failed")
            skipped_count += 1
            continue
 
        success = False
        posted_ids = []
 
        for page_key in pages_to_post:
            page = PAGES[page_key]
            log(f"    Đăng lên {page_key} ({page['page_id']})...")
            post_id = upload_video_to_fb(
                page["page_id"],
                page["page_token"],
                video_source,
                title,
                desc,
                thumb if thumb else None,
                unix_ts
            )
            if post_id:
                success = True
                posted_ids.append(post_id)
                log(f"    → Thành công! Post ID: {post_id}")
            else:
                log(f"    → Thất bại (xem lỗi phía trên)")
 
        # Update kết quả cuối
        new_status = "Posted" if success else "Failed"
        sheet.update_cell(row_idx, col_status, new_status)
 
        if success and posted_ids:
            sheet.update_cell(row_idx, col_post_id, ", ".join(posted_ids))
            log(f"  Row {row_idx} → Hoàn tất | Status: {new_status} | IDs: {', '.join(posted_ids)}")
        else:
            log(f"  Row {row_idx} → Hoàn tất | Status: {new_status}")
 
    log(f"Kết thúc quét | Xử lý: {processed_count} row | Skip: {skipped_count} row")
    log("-" * 60)
 
 
# === SCHEDULER (múi giờ Việt Nam) ===
scheduler = BlockingScheduler(timezone="Asia/Ho_Chi_Minh")
 
# Chạy trước slot 5 phút + quét interval 5 phút
scheduler.add_job(process_sheet, 'cron', hour='12,20', minute='55')
scheduler.add_job(process_sheet, 'interval', minutes=5)
 
log("Bot khởi động | Quét mỗi 5 phút + trigger lúc 12:55 / 20:55 (VN)")
log("Log được ghi vào file: fb_video_bot.log")
try:
    scheduler.start()
except (KeyboardInterrupt, SystemExit):
    log("Bot dừng bởi người dùng")