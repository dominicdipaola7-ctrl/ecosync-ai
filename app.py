import os
import json
import uuid
import io
from datetime import datetime, date
from flask import (
    Flask, render_template, request, jsonify,
    session, send_file, abort
)
from flask_session import Session
from dotenv import load_dotenv
import anthropic
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "eco-sync-dev-secret-2024")
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = "./flask_session"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True

os.makedirs("./flask_session", exist_ok=True)
Session(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

FREE_TIER_LIMIT = 3

def get_store():
    if "patients" not in session:
        session["patients"] = {}
    if "sessions" not in session:
        session["sessions"] = {}
    if "reports" not in session:
        session["reports"] = {}
    if "report_count" not in session:
        session["report_count"] = 0
    if "tier" not in session:
        session["tier"] = "free"
    return session


@app.route("/")
def index():
    store = get_store()
    return render_template("index.html",
                           patients=store["patients"],
                           sessions=store["sessions"],
                           reports=store["reports"],
                           report_count=store["report_count"],
                           tier=store["tier"],
                           free_limit=FREE_TIER_LIMIT)


@app.route("/upgrade", methods=["POST"])
def upgrade():
    store = get_store()
    store["tier"] = "pro"
    session.modified = True
    return jsonify({"success": True, "tier": "pro"})


@app.route("/api/patients", methods=["GET"])
def list_patients():
    store = get_store()
    return jsonify(list(store["patients"].values()))


@app.route("/api/patients", methods=["POST"])
def create_patient():
    store = get_store()
    data = request.get_json()

    if store["tier"] == "free" and len(store["patients"]) >= 1:
        return jsonify({"error": "Free tier limited to 1 patient. Upgrade to Pro for multi-patient management."}), 403

    pid = str(uuid.uuid4())[:8]
    patient = {
        "id": pid,
        "name": data.get("name", "Unknown"),
        "dob": data.get("dob", ""),
        "diagnosis": data.get("diagnosis", ""),
        "hrv_baseline": float(data.get("hrv_baseline", 50)),
        "stress_threshold": float(data.get("stress_threshold", 70)),
        "resting_hr": float(data.get("resting_hr", 72)),
        "notes": data.get("notes", ""),
        "created_at": datetime.utcnow().isoformat()
    }
    store["patients"][pid] = patient
    session.modified = True
    return jsonify(patient), 201


@app.route("/api/patients/<pid>", methods=["GET"])
def get_patient(pid):
    store = get_store()
    p = store["patients"].get(pid)
    if not p:
        abort(404)
    return jsonify(p)


@app.route("/api/patients/<pid>", methods=["DELETE"])
def delete_patient(pid):
    store = get_store()
    if pid in store["patients"]:
        del store["patients"][pid]
        session.modified = True
    return jsonify({"success": True})


@app.route("/api/sessions", methods=["POST"])
def create_session_record():
    store = get_store()

    if store["tier"] == "free" and store["report_count"] >= FREE_TIER_LIMIT:
        return jsonify({
            "error": f"Free tier limit of {FREE_TIER_LIMIT} session reports reached. Upgrade to Pro for unlimited reports."
        }), 403

    data = request.get_json()
    pid = data.get("patient_id")
    if pid not in store["patients"]:
        return jsonify({"error": "Patient not found"}), 404

    sid = str(uuid.uuid4())[:8]
    events = data.get("stress_events", [])
    env_tags = data.get("environment_tags", [])

    pre_hrv = float(data.get("pre_session_hrv", 0))
    post_hrv = float(data.get("post_session_hrv", 0))
    pre_stress = float(data.get("pre_session_stress", 0))
    post_stress = float(data.get("post_session_stress", 0))

    hrv_delta = post_hrv - pre_hrv
    stress_delta = pre_stress - post_stress

    ror = 0.0
    if pre_stress > 0:
        ror = round((stress_delta / pre_stress) * 100, 1)

    sess = {
        "id": sid,
        "patient_id": pid,
        "date": data.get("date", date.today().isoformat()),
        "duration_minutes": int(data.get("duration_minutes", 60)),
        "environment": data.get("environment", "forest"),
        "environment_tags": env_tags,
        "pre_session_hrv": pre_hrv,
        "post_session_hrv": post_hrv,
        "pre_session_stress": pre_stress,
        "post_session_stress": post_stress,
        "hrv_delta": round(hrv_delta, 1),
        "stress_delta": round(stress_delta, 1),
        "rate_of_regulation": ror,
        "stress_events": events,
        "clinician_notes": data.get("clinician_notes", ""),
        "interventions": data.get("interventions", []),
        "created_at": datetime.utcnow().isoformat(),
        "report_generated": False
    }
    store["sessions"][sid] = sess
    session.modified = True
    return jsonify(sess), 201


@app.route("/api/sessions/<pid>", methods=["GET"])
def get_sessions_for_patient(pid):
    store = get_store()
    patient_sessions = [
        s for s in store["sessions"].values()
        if s["patient_id"] == pid
    ]
    patient_sessions.sort(key=lambda x: x["date"])
    return jsonify(patient_sessions)


@app.route("/api/reports/generate", methods=["POST"])
def generate_report():
    store = get_store()

    if store["tier"] == "free" and store["report_count"] >= FREE_TIER_LIMIT:
        return jsonify({
            "error": f"Free tier limit of {FREE_TIER_LIMIT} reports reached. Please upgrade to Pro."
        }), 403

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Anthropic API key not configured. Please set ANTHROPIC_API_KEY in environment."}), 500

    data = request.get_json()
    sid = data.get("session_id")
    sess = store["sessions"].get(sid)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    patient = store["patients"].get(sess["patient_id"])
    if not patient:
        return jsonify({"error": "Patient not found"}), 404

    prompt = _build_report_prompt(patient, sess)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        report_text = message.content[0].text
    except Exception as e:
        return jsonify({"error": f"AI generation failed: {str(e)}"}), 500

    rid = str(uuid.uuid4())[:8]
    report = {
        "id": rid,
        "session_id": sid,
        "patient_id": sess["patient_id"],
        "patient_name": patient["name"],
        "session_date": sess["date"],
        "generated_at": datetime.utcnow().isoformat(),
        "content": report_text,
        "environment": sess["environment"],
        "rate_of_regulation": sess["rate_of_regulation"],
        "hrv_delta": sess["hrv_delta"]
    }

    store["reports"][rid] = report
    store["sessions"][sid]["report_generated"] = True
    store["report_count"] = store.get("report_count", 0) + 1
    session.modified = True

    return jsonify(report), 201


def _build_report_prompt(patient, sess):
    events_text = "\n".join(
        [f"  - {e.get('time', 'N/A')} | Stress Level {e.get('level', '?')}/10 | Note: {e.get('note', '')}"
         for e in sess.get("stress_events", [])]
    ) or "  No discrete stress events recorded."

    interventions = ", ".join(sess.get("interventions", [])) or "Standard nature immersion"
    env_tags = ", ".join(sess.get("environment_tags", [])) or sess.get("environment", "forest")

    return f"""You are a licensed clinical documentation specialist with expertise in nature-based therapy and remote therapeutic monitoring (RTM). Generate a formal Clinical Necessity Report suitable for insurance submission under CPT codes 98975-98981.

PATIENT INFORMATION:
- Name: {patient['name']}
- Date of Birth: {patient.get('dob', 'Not provided')}
- Primary Diagnosis: {patient.get('diagnosis', 'Not specified')}
- HRV Baseline: {patient['hrv_baseline']} ms
- Stress Threshold: {patient['stress_threshold']}/100
- Resting Heart Rate Baseline: {patient.get('resting_hr', 72)} bpm

SESSION DATA:
- Date: {sess['date']}
- Duration: {sess['duration_minutes']} minutes
- Environment Type: {sess['environment'].upper()}
- Environment Tags: {env_tags}
- Pre-Session HRV: {sess['pre_session_hrv']} ms
- Post-Session HRV: {sess['post_session_hrv']} ms
- HRV Delta: {sess['hrv_delta']:+.1f} ms
- Pre-Session Stress Score: {sess['pre_session_stress']}/100
- Post-Session Stress Score: {sess['post_session_stress']}/100
- Rate of Regulation (ROR): {sess['rate_of_regulation']}%
- Interventions Used: {interventions}
- Clinician Notes: {sess.get('clinician_notes', 'None provided')}

STRESS EVENTS DURING SESSION:
{events_text}

Generate a complete Clinical Necessity Report with ALL of the following clearly labeled sections:

1. CLINICAL NECESSITY STATEMENT
A formal 2-3 paragraph statement justifying the medical necessity of nature-based RTM therapy for this patient, referencing specific biometric data.

2. SESSION SUMMARY & PHYSIOLOGICAL FINDINGS
Detailed clinical summary of session outcomes including HRV analysis, stress regulation efficacy, and environmental correlation findings.

3. ENVIRONMENT-OUTCOME CORRELATION ANALYSIS
Clinical analysis of how the specific environment ({sess['environment']}) contributed to physiological regulation. Reference peer-supported mechanisms (e.g., attentional restoration theory, stress recovery theory, autonomic nervous system modulation).

4. RTM MONITORING DATA SUMMARY (CPT 98975-98981)
Structured table-ready data summary formatted for RTM billing codes:
- CPT 98975: Initial setup and patient education
- CPT 98976/98977: Device supply with daily recordings
- CPT 98980: First 20 minutes of RTM treatment management
- CPT 98981: Each additional 20 minutes

5. TREATMENT RESPONSE & PROGRESS INDICATORS
Clinical interpretation of the Rate of Regulation score ({sess['rate_of_regulation']}%) and what it indicates about treatment trajectory.

6. CLINICAL RECOMMENDATIONS
Specific, actionable recommendations for next session including environment selection, duration, and biometric targets.

7. ATTESTATION LANGUAGE
Standard clinician attestation paragraph suitable for insurance submission.

Use formal clinical language throughout. Be specific and data-driven. Format the report clearly with section headers."""


@app.route("/api/reports/<rid>/pdf", methods=["GET"])
def download_pdf(rid):
    store = get_store()
    report = store["reports"].get(rid)
    if not report:
        abort(404)

    patient = store["patients"].get(report["patient_id"], {})
    pdf_buffer = _generate_pdf(report, patient)

    filename = f"EcoSync_Report_{report['patient_name'].replace(' ', '_')}_{report['session_date']}.pdf"
    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


def _generate_pdf(report, patient):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch
    )

    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Title"],
        fontSize=20,
        textColor=colors.HexColor("#0f4c35"),
        spaceAfter=4,
        alignment=TA_CENTER,
        fontName="Helvetica-Bold"
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#4a9e7f"),
        spaceAfter=2,
        alignment=TA_CENTER
    )
    section_header_style = ParagraphStyle(
        "SectionHeader",
        parent=styles["Heading1"],
        fontSize=11,
        textColor=colors.HexColor("#0f4c35"),
        spaceBefore=14,
        spaceAfter=6,
        fontName="Helvetica-Bold",
        borderPad=4,
        leftIndent=0
    )
    body_style = ParagraphStyle(
        "BodyText",
        parent=styles["Normal"],
        fontSize=9,
        leading=14,
        spaceAfter=6,
        alignment=TA_JUSTIFY
    )
    meta_style = ParagraphStyle(
        "Meta",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#666666"),
        spaceAfter=2
    )

    story.append(Paragraph("Eco-Sync AI", title_style))
    story.append(Paragraph("Nature-Based Therapy | Remote Therapeutic Monitoring", subtitle_style))
    story.append(Paragraph("Clinical Necessity Report - Insurance Submission Document", subtitle_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#0f4c35")))
    story.append(Spacer(1, 0.1 * inch))

    meta_data = [
        ["Patient:", report.get("patient_name", "N/A"), "Report ID:", report["id"]],
        ["Session Date:", report.get("session_date", "N/A"), "Generated:", report.get("generated_at", "N/A")[:10]],
        ["Environment:", report.get("environment", "N/A").upper(), "ROR Score:", f"{report.get('rate_of_regulation', 0)}%"],
        ["HRV Delta:", f"{report.get('hrv_delta', 0):+.1f} ms", "CPT Codes:", "98975-98981"],
    ]
    meta_table = Table(meta_data, colWidths=[1.2 * inch, 2.3 * inch, 1.2 * inch, 2.3 * inch])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#0f4c35")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#0f4c35")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f7f4")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#f0f7f4"), colors.HexColor("#e8f4ef")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8e0d8")),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.15 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c8e0d8")))

    content = report.get("content", "")
    lines = content.split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            story.append(Spacer(1, 0.05 * inch))
            continue

        if (line and (
            (line[0].isdigit() and len(line) > 2 and line[1] in ".):" ) or
            line.isupper() or
            (line.startswith("**") and line.endswith("**"))
        )):
            clean = line.strip("*").strip("0123456789.): ").strip()
            if len(clean) > 5:
                story.append(Paragraph(line.strip("*"), section_header_style))
                story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#4a9e7f"), spaceAfter=4))
                continue

        if line.startswith("- ") or line.startswith("* "):
            story.append(Paragraph(f"&bull; {line[2:]}", body_style))
        else:
            story.append(Paragraph(line, body_style))

    story.append(Spacer(1, 0.2 * inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#0f4c35")))
    story.append(Spacer(1, 0.05 * inch))
    footer_text = (
        f"This report was generated by Eco-Sync AI on {report.get('generated_at', '')[:10]}. "
        "This document is intended for clinical and insurance submission use only. "
        "Eco-Sync AI | Nature-Based Remote Therapeutic Monitoring Platform | CPT 98975-98981"
    )
    story.append(Paragraph(footer_text, meta_style))

    doc.build(story)
    buffer.seek(0)
    return buffer


@app.route("/api/analytics/<pid>", methods=["GET"])
def get_analytics(pid):
    store = get_store()
    patient_sessions = [
        s for s in store["sessions"].values()
        if s["patient_id"] == pid
    ]
    patient_sessions.sort(key=lambda x: x["date"])

    chart_data = {
        "labels": [s["date"] for s in patient_sessions],
        "ror": [s["rate_of_regulation"] for s in patient_sessions],
        "hrv": [s["post_session_hrv"] for s in patient_sessions],
        "stress_post": [s["post_session_stress"] for s in patient_sessions],
        "environments": [s["environment"] for s in patient_sessions]
    }
    return jsonify(chart_data)


@app.route("/api/demo/load", methods=["POST"])
def load_demo():
    store = get_store()
    store["patients"] = {}
    store["sessions"] = {}
    store["reports"] = {}
    store["report_count"] = 0

    pid = "demo0001"
    store["patients"][pid] = {
        "id": pid,
        "name": "Jane Doe (Demo)",
        "dob": "1985-04-12",
        "diagnosis": "Generalized Anxiety Disorder (F41.1)",
        "hrv_baseline": 48.0,
        "stress_threshold": 65.0,
        "resting_hr": 74.0,
        "notes": "Patient reports elevated work-related stress. Referred for nature-based RTM.",
        "created_at": "2024-01-15T09:00:00"
    }

    demo_sessions = [
        {"date": "2024-02-01", "env": "forest", "pre_hrv": 41, "post_hrv": 52, "pre_s": 78, "post_s": 55, "ror": 29.5, "notes": "First session. Patient initially anxious.", "tags": ["pine trees", "bird sounds", "soft trail"]},
        {"date": "2024-02-08", "env": "water", "pre_hrv": 44, "post_hrv": 58, "pre_s": 72, "post_s": 46, "ror": 36.1, "notes": "Water proximity showed strong regulation response.", "tags": ["stream", "running water", "mossy rocks"]},
        {"date": "2024-02-15", "env": "forest", "pre_hrv": 47, "post_hrv": 61, "pre_s": 69, "post_s": 41, "ror": 40.6, "notes": "Patient self-reported feeling 'grounded'. HRV responding well.", "tags": ["old growth", "canopy", "wildlife sounds"]},
        {"date": "2024-02-22", "env": "open field", "pre_hrv": 50, "post_hrv": 63, "pre_s": 65, "post_s": 38, "ror": 41.5, "notes": "Open field + movement integration. Best session yet.", "tags": ["meadow", "open sky", "gentle breeze"]},
        {"date": "2024-03-01", "env": "water", "pre_hrv": 52, "post_hrv": 67, "pre_s": 61, "post_s": 34, "ror": 44.3, "notes": "Consistent improvement. Patient initiated breathing exercises unprompted.", "tags": ["lake", "reflective surface", "quiet"]},
    ]

    for i, s in enumerate(demo_sessions):
        sid = f"ds{i + 1:04d}"
        hrv_delta = s["post_hrv"] - s["pre_hrv"]
        stress_delta = s["pre_s"] - s["post_s"]
        store["sessions"][sid] = {
            "id": sid,
            "patient_id": pid,
            "date": s["date"],
            "duration_minutes": 60,
            "environment": s["env"],
            "environment_tags": s["tags"],
            "pre_session_hrv": s["pre_hrv"],
            "post_session_hrv": s["post_hrv"],
            "pre_session_stress": s["pre_s"],
            "post_session_stress": s["post_s"],
            "hrv_delta": round(hrv_delta, 1),
            "stress_delta": round(stress_delta, 1),
            "rate_of_regulation": s["ror"],
            "stress_events": [
                {"time": "00:10", "level": "7", "note": "Entry anxiety spike"},
                {"time": "00:25", "level": "4", "note": "Regulation beginning"},
                {"time": "00:50", "level": "2", "note": "Full regulation achieved"}
            ],
            "clinician_notes": s["notes"],
            "interventions": ["Mindful walking", "Breath awareness", "Sensory grounding"],
            "created_at": f"{s['date']}T10:00:00",
            "report_generated": False
        }

    session.modified = True
    return jsonify({"success": True, "patient_id": pid})


if __name__ == "__main__":
    app.run(debug=True, port=5000)