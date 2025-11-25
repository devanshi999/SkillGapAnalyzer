from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import io
import fitz                     
import docx2txt                
import os
import csv
from typing import List, Dict, Tuple
from rapidfuzz import fuzz
import tempfile

app = FastAPI(title="Skill Gap Analyzer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "Skill Gap Analyzer Backend Running"}

SKILLS_CSV = "skills.csv"   
FUZZY_THRESHOLD_STRONG = 80
FUZZY_THRESHOLD_WEAK = 60
MIN_OCCURRENCE_FOR_STRONG = 2

def extract_text_from_pdf(file_bytes: bytes) -> str:
    text_chunks = []
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for page in doc:
            text_chunks.append(page.get_text())
    return "\n".join(text_chunks)


def extract_text_from_docx(file_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        tmp.write(file_bytes)
        path = tmp.name
    try:
        return docx2txt.process(path) or ""
    finally:
        try:
            os.unlink(path)
        except:
            pass


def extract_text_from_txt(file_bytes: bytes) -> str:
    return file_bytes.decode("utf-8", errors="ignore")


def extract_text(upload_file: UploadFile) -> str:
    contents = upload_file.file.read()
    upload_file.file.seek(0)

    name = upload_file.filename.lower()

    if name.endswith(".pdf"):
        return extract_text_from_pdf(contents)
    if name.endswith(".docx"):
        return extract_text_from_docx(contents)
    if name.endswith(".txt"):
        return extract_text_from_txt(contents)

    # fallback attempts
    try:
        return extract_text_from_pdf(contents)
    except:
        try:
            return extract_text_from_docx(contents)
        except:
            return extract_text_from_txt(contents)

def load_skills_from_csv(path: str) -> List[str]:
    skills = []
    if not os.path.exists(path):
        return skills

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if row:
                skill = row[0].strip()
                if skill:
                    skills.append(skill)
    return skills


def find_skill_evidence(skill: str, text: str) -> Tuple[int, float]:
    text_low = text.lower()
    skill_low = skill.lower()

    occ = text_low.count(skill_low)
    best_score = 0.0

    lines = [s.strip() for s in text.split("\n") if s.strip()]
    for line in lines:
        score = fuzz.token_set_ratio(skill_low, line.lower())
        if score > best_score:
            best_score = score

    return occ, float(best_score)


def extract_skills_from_text(text: str, skills_list: List[str]) -> List[Dict]:
    result = []
    for skill in skills_list:
        occ, score = find_skill_evidence(skill, text)
        result.append({
            "skill": skill,
            "occurrences": occ,
            "best_score": score
        })
    return result

@app.post("/analyze")
async def analyze(resume: UploadFile = File(...), job_description: UploadFile = File(...)):

    # Extract text
    try:
        resume_text = extract_text(resume)
    except Exception as e:
        raise HTTPException(500, f"Error reading resume: {e}")

    try:
        jd_text = extract_text(job_description)
    except Exception as e:
        raise HTTPException(500, f"Error reading job description: {e}")

    # Load skills
    skills = load_skills_from_csv(SKILLS_CSV)
    if not skills:
        return {
            "warning": "skills.csv not found or empty",
            "resume_snippet": resume_text[:500],
            "jd_snippet": jd_text[:500]
        }

    jd_info = extract_skills_from_text(jd_text, skills)

    jd_required = []
    for s in jd_info:
        if s["occurrences"] > 0 or s["best_score"] >= FUZZY_THRESHOLD_WEAK:
            jd_required.append(s)

    resume_info = extract_skills_from_text(resume_text, skills)
    resume_lookup = {s["skill"].lower(): s for s in resume_info}

    comparison = []
    for jd_skill in jd_required:
        skill_name = jd_skill["skill"]
        resume_skill = resume_lookup.get(skill_name.lower(), {"occurrences": 0, "best_score": 0})

        occ = resume_skill["occurrences"]
        score = resume_skill["best_score"]

        if score >= FUZZY_THRESHOLD_STRONG or occ >= MIN_OCCURRENCE_FOR_STRONG:
            status = "present"
        elif score >= FUZZY_THRESHOLD_WEAK or occ == 1:
            status = "weak"
        else:
            status = "missing"

        evidence = []
        for line in resume_text.split("\n"):
            if skill_name.lower() in line.lower() or fuzz.token_set_ratio(skill_name.lower(), line.lower()) >= FUZZY_THRESHOLD_WEAK:
                evidence.append(line.strip())
                if len(evidence) >= 5:
                    break

        comparison.append({
            "skill": skill_name,
            "status": status,
            "jd_best_score": jd_skill["best_score"],
            "resume_best_score": score,
            "resume_occurrences": occ,
            "evidence": evidence
        })

    total = len(jd_required)
    present = sum(1 for c in comparison if c["status"] == "present")
    weak = sum(1 for c in comparison if c["status"] == "weak")
    missing = sum(1 for c in comparison if c["status"] == "missing")

    gap_score = round(100 * (1 - (present + 0.5 * weak) / max(1, total)), 1)

    return {
        "summary": {
            "total_required": total,
            "present": present,
            "weak": weak,
            "missing": missing,
            "gap_score_percent": gap_score
        },
        "comparison": comparison,
        "metadata": {
            "resume_filename": resume.filename,
            "job_description_filename": job_description.filename
        }
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
