# Auto-post-bot
Python bot for automatically posting scheduled Facebook Page videos from Google Sheets, with multi-page support, logging, and post-status updates.

This project is suitable for content operations such as sales posting, page growth, video seeding, or managing multiple fan pages without having to post manually every day.

## Key Features

- Automatically scans Google Sheets for videos queued for posting
- Supports posting videos from:
  - URL links
  - Google Drive links converted to direct download
  - Local files stored on the machine/server
- Supports posting to one or multiple Facebook Pages
- Automatically checks date, time, and status before posting
- Updates post status back to Google Sheets
- Saves Post ID after successful posting
- Includes logging for tracking progress and errors
- Scheduler runs every 5 minutes and triggers before configured posting time slots

## How It Works

The bot reads data from Google Sheets and checks rows that match:

- the current date
- the configured posting time
- a valid status for posting

When the correct time is reached, the bot will:
1. lock the row being processed to avoid duplicate posting
2. upload the video to the Facebook Page
3. update the status to `Posted` or `Failed`
4. save the `Post ID` into the sheet if the post is successful

## Technologies Used

- Python
- Facebook Graph API
- Google Sheets API
- gspread
- APScheduler
- python-dotenv

## Environment Variables

The project uses a `.env` file for configuration, for example:

```env
APP_ID=your_app_id
USER_ACCESS_TOKEN=your_user_access_token
SERVICE_ACCOUNT_FILE=path_to_service_account.json

SHEET_NAME=AutoPost Video Bot - Content Queue
WORKSHEET_NAME=Video Queue

PAGE1_ID=your_page_id
PAGE1_TOKEN=your_page_token

PAGE2_ID=your_second_page_id
PAGE2_TOKEN=your_second_page_token
