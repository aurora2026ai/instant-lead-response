#!/usr/bin/env python3
"""
Instant Lead Response System
Responds to leads in <60 seconds with AI-personalized messages
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
import anthropic
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Configuration
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "leads.db"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Email config (will use environment variables)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

# Telegram notification
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

app = FastAPI(title="Instant Lead Response System")


class LeadSubmission(BaseModel):
    """Lead form data model"""
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    company: str = Field(..., min_length=2, max_length=100)
    message: str = Field(..., min_length=10, max_length=1000)
    phone: Optional[str] = Field(None, max_length=20)


class LeadResponse(BaseModel):
    """API response model"""
    success: bool
    message: str
    lead_id: Optional[int] = None
    response_time_ms: Optional[int] = None


# Database setup
def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            company TEXT NOT NULL,
            message TEXT NOT NULL,
            phone TEXT,
            lead_score INTEGER,
            intent_classification TEXT,
            response_time_ms INTEGER,
            email_sent BOOLEAN,
            ai_response TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def save_lead(lead_data: Dict[str, Any]) -> int:
    """Save lead to database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO leads (
            timestamp, name, email, company, message, phone,
            lead_score, intent_classification, response_time_ms,
            email_sent, ai_response
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        lead_data['timestamp'],
        lead_data['name'],
        lead_data['email'],
        lead_data['company'],
        lead_data['message'],
        lead_data.get('phone'),
        lead_data['lead_score'],
        lead_data['intent_classification'],
        lead_data['response_time_ms'],
        lead_data['email_sent'],
        lead_data['ai_response']
    ))
    lead_id = c.lastrowid
    conn.commit()
    conn.close()
    return lead_id


def get_stats() -> Dict[str, Any]:
    """Get system statistics"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Total leads
    c.execute('SELECT COUNT(*) FROM leads')
    total_leads = c.fetchone()[0]

    # Average response time
    c.execute('SELECT AVG(response_time_ms) FROM leads WHERE response_time_ms IS NOT NULL')
    avg_response = c.fetchone()[0] or 0

    # Email success rate
    c.execute('SELECT COUNT(*) FROM leads WHERE email_sent = 1')
    emails_sent = c.fetchone()[0]

    # Recent leads (last 10)
    c.execute('''
        SELECT name, company, lead_score, response_time_ms, created_at
        FROM leads
        ORDER BY created_at DESC
        LIMIT 10
    ''')
    recent_leads = [
        {
            'name': row[0],
            'company': row[1],
            'score': row[2],
            'response_time_ms': row[3],
            'timestamp': row[4]
        }
        for row in c.fetchall()
    ]

    conn.close()

    return {
        'total_leads': total_leads,
        'avg_response_time_ms': int(avg_response) if avg_response else 0,
        'emails_sent': emails_sent,
        'email_success_rate': (emails_sent / total_leads * 100) if total_leads > 0 else 0,
        'recent_leads': recent_leads
    }


def process_lead_with_ai(lead: LeadSubmission) -> Dict[str, Any]:
    """
    Process lead with Claude AI:
    - Classify intent
    - Score lead quality (1-10)
    - Generate personalized response
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are a lead response AI. Analyze this lead and respond:

Lead Information:
- Name: {lead.name}
- Company: {lead.company}
- Email: {lead.email}
- Message: {lead.message}
{f'- Phone: {lead.phone}' if lead.phone else ''}

Tasks:
1. Classify the intent (demo_request, pricing_inquiry, support_question, partnership, general_inquiry)
2. Score the lead quality 1-10 (based on message clarity, company presence, urgency signals)
3. Generate a warm, personalized response email (2-3 paragraphs) that:
   - Addresses their specific question/need
   - Demonstrates you understood their message
   - Provides next steps (calendar link, demo info, etc.)
   - Signs off as "Aurora - Lead Response AI"

Respond in this JSON format:
{{
  "intent": "intent_classification",
  "score": 8,
  "response": "Email body here..."
}}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    # Parse AI response
    response_text = message.content[0].text

    # Extract JSON (handle potential markdown wrapping)
    if '```json' in response_text:
        response_text = response_text.split('```json')[1].split('```')[0].strip()
    elif '```' in response_text:
        response_text = response_text.split('```')[1].split('```')[0].strip()

    ai_result = json.loads(response_text)

    return {
        'intent_classification': ai_result.get('intent', 'unknown'),
        'lead_score': ai_result.get('score', 5),
        'ai_response': ai_result.get('response', '')
    }


def send_email_response(to_email: str, to_name: str, company: str, response_body: str) -> bool:
    """Send email response via SMTP"""
    if not all([SMTP_USER, SMTP_PASSWORD]):
        print("SMTP not configured, skipping email send")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Re: Your inquiry from {company}"
        msg['From'] = f"Aurora Lead Response <{FROM_EMAIL}>"
        msg['To'] = to_email

        # Plain text version
        text_part = MIMEText(response_body, 'plain')
        msg.attach(text_part)

        # Send
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        return True
    except Exception as e:
        print(f"Email send failed: {e}")
        return False


def send_telegram_notification(lead_data: Dict[str, Any]):
    """Send Telegram notification to sales team"""
    if not TELEGRAM_CHAT_ID:
        return

    try:
        import subprocess
        message = f"""🔔 New Lead Alert

👤 {lead_data['name']} from {lead_data['company']}
📧 {lead_data['email']}
🎯 Intent: {lead_data['intent_classification']}
⭐ Score: {lead_data['lead_score']}/10
⚡ Response: {lead_data['response_time_ms']}ms

Message: {lead_data['message'][:100]}..."""

        subprocess.run(
            ['python3', '/opt/autonomous-ai/send_telegram.py', '-m', message],
            capture_output=True,
            timeout=5
        )
    except Exception as e:
        print(f"Telegram notification failed: {e}")


# API Endpoints

@app.post("/api/submit-lead", response_model=LeadResponse)
async def submit_lead(lead: LeadSubmission):
    """
    Main endpoint: Receive lead, process with AI, respond via email
    Target: <60 second total response time
    """
    start_time = time.time()

    try:
        # Step 1: Process with AI (classify intent, score, generate response)
        ai_result = process_lead_with_ai(lead)

        # Step 2: Send email response
        email_sent = send_email_response(
            to_email=lead.email,
            to_name=lead.name,
            company=lead.company,
            response_body=ai_result['ai_response']
        )

        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)

        # Step 3: Save to database
        lead_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'name': lead.name,
            'email': lead.email,
            'company': lead.company,
            'message': lead.message,
            'phone': lead.phone,
            'lead_score': ai_result['lead_score'],
            'intent_classification': ai_result['intent_classification'],
            'response_time_ms': response_time_ms,
            'email_sent': email_sent,
            'ai_response': ai_result['ai_response']
        }

        lead_id = save_lead(lead_data)

        # Step 4: Notify sales team
        send_telegram_notification(lead_data)

        return LeadResponse(
            success=True,
            message=f"Thank you! We've responded to {lead.email} in {response_time_ms}ms",
            lead_id=lead_id,
            response_time_ms=response_time_ms
        )

    except Exception as e:
        print(f"Error processing lead: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats")
async def get_system_stats():
    """Get system statistics"""
    return get_stats()


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve landing page"""
    html_path = BASE_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())

    return HTMLResponse(content="""
    <html>
        <head><title>Instant Lead Response System</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
            <h1>⚡ Instant Lead Response System</h1>
            <p>API is running. Add index.html for full landing page.</p>
            <p><a href="/api/stats">View Stats</a></p>
        </body>
    </html>
    """)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    init_db()
    print("✅ Lead Response System started")
    print(f"📊 Database: {DB_PATH}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
