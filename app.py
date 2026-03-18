from groq import Groq
import os
import base64
from docx import Document
from docx.shared import Pt, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import io
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz
import pdfplumber

app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:5173",
    "http://localhost:5174",
    "https://paperfinder-pro.vercel.app"
])

# ── Helper functions ─────────────────────────────────────────

def clean_text(text):
    text = re.sub(r'[^\x20-\x7E\n]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_query(text):
    stop_words = {
        'the','and','for','with','from','this','that','are','was','were','has',
        'have','been','will','can','may','our','their','also','which','when',
        'where','how','what','all','not','but','its','than','more','some','such',
        'each','both','only','very','most','into','over','after','under','about',
        'other','these','those','then','them','they','would','could','should',
        'while','between','through','during','before','any','use','used','using',
        'based','show','shows','paper','study','research','article','results',
        'result','method','methods','data','model','models','figure','table',
        'section','university','college','china','shanghai','received','accepted',
        'published','copyright','journal','volume','correspondence','academic',
        'address','revised','april','march','october','january','february','june',
        'july','august','september','november','december','email','doi','http',
        'www','page','pages'
    }
    words = text.split()
    keywords = [w for w in words if len(w) > 4 and w.isalpha() and w.lower() not in stop_words]
    seen = set()
    unique_keywords = []
    for w in keywords:
        lower = w.lower()
        if lower not in seen:
            seen.add(lower)
            unique_keywords.append(w)
        if len(unique_keywords) == 12:
            break
    return ' '.join(unique_keywords)

def extract_with_pymupdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""
    for page_num in range(min(2, len(doc))):
        text += doc[page_num].get_text() + " "
    doc.close()
    return clean_text(text)

def extract_with_pdfplumber(pdf_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:2]:
            page_text = page.extract_text()
            if page_text:
                text += page_text + " "
    return clean_text(text)

def set_two_columns(section):
    sectPr = section._sectPr
    for existing in sectPr.findall(qn('w:cols')):
        sectPr.remove(existing)
    cols = OxmlElement('w:cols')
    cols.set(qn('w:num'), '2')
    cols.set(qn('w:space'), '720')
    cols.set(qn('w:equalWidth'), '1')
    sectPr.append(cols)

def insert_continuous_section_break(doc, two_col=True):
    para = doc.add_paragraph()
    pPr = para._p.get_or_add_pPr()
    sectPr = OxmlElement('w:sectPr')
    pgType = OxmlElement('w:type')
    pgType.set(qn('w:val'), 'continuous')
    sectPr.append(pgType)
    cols = OxmlElement('w:cols')
    if two_col:
        cols.set(qn('w:num'), '2')
        cols.set(qn('w:space'), '720')
        cols.set(qn('w:equalWidth'), '1')
    else:
        cols.set(qn('w:num'), '1')
    sectPr.append(cols)
    pPr.append(sectPr)

def clean_line(line):
    # Fix: properly escaped regex for bold/italic markdown removal
    line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
    line = re.sub(r'\*(.+?)\*', r'\1', line)
    line = re.sub(r'#{1,6}\s*', '', line)
    return line.strip()

def is_ieee_section_heading(line):
    """Detects IEEE-style headings: I. INTRODUCTION, II. RELATED WORK, etc."""
    return bool(re.match(r'^[IVXivx]+\.\s+[A-Z\s]{3,}$', line.strip()))

def is_ieee_subsection_heading(line):
    """Detects IEEE subsection: A. Name, B. Name"""
    return bool(re.match(r'^[A-Z]\.\s+\w', line.strip())) and len(line) < 80

def is_generic_section_heading(line, keywords):
    """Fallback heading detection for non-IEEE formats."""
    clean = re.sub(r'^[IVXivx]+\.\s*|^\d+\.?\s*', '', line.lower()).strip().rstrip('.')
    return any(clean.startswith(kw) for kw in keywords) and len(line) < 80

def is_reference_line(line):
    return bool(re.match(r'^\[\d+\]', line.strip()))

def is_figure_placeholder(line):
    upper = line.upper()
    return '[DIAGRAM_HERE' in upper or '[FIGURE' in upper

# ── Routes ───────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "PaperFinder backend running"})


@app.route('/extract-pdf', methods=['POST'])
def extract_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    if not file.filename.endswith('.pdf'):
        return jsonify({"error": "File must be a PDF"}), 400
    pdf_bytes = file.read()
    try:
        text = extract_with_pymupdf(pdf_bytes)
        if text and len(text.split()) > 15:
            return jsonify({"success": True, "query": extract_query(text), "method": "pymupdf", "preview": text[:200]})
    except Exception as e:
        print(f"PyMuPDF failed: {e}")
    try:
        text = extract_with_pdfplumber(pdf_bytes)
        if text and len(text.split()) > 15:
            return jsonify({"success": True, "query": extract_query(text), "method": "pdfplumber", "preview": text[:200]})
    except Exception as e:
        print(f"pdfplumber failed: {e}")
    return jsonify({"error": "Could not extract text from this PDF."}), 422


@app.route('/extract-pdf-full', methods=['POST'])
def extract_pdf_full():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    if not file.filename.endswith('.pdf'):
        return jsonify({"error": "File must be a PDF"}), 400
    pdf_bytes = file.read()
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        pages = len(doc)
        doc.close()
        text = clean_text(text)
        if not text or len(text.split()) < 10:
            raise Exception("Could not extract text from this PDF")
        return jsonify({"success": True, "text": text[:15000], "pages": pages})
    except Exception as e:
        return jsonify({"error": str(e)}), 422


@app.route('/chat-pdf', methods=['POST'])
def chat_pdf():
    question = request.form.get('question', '').strip()
    pdf_text  = request.form.get('pdf_text', '').strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400
    if not pdf_text:
        return jsonify({"error": "No PDF text provided"}), 400

    prompt = f"""You are a helpful research assistant. A user has uploaded a PDF and wants to ask questions about it.

PDF CONTENT:
{pdf_text[:8000]}

USER QUESTION:
{question}

INSTRUCTIONS:
- Answer ONLY based on the PDF content above
- Be specific and reference relevant sections when helpful
- If the answer is not found in the PDF, clearly say so
- Keep answers clear, concise and academic
- Format your answer with proper paragraphs
- If the question asks for a summary, provide a structured summary

Answer:"""

    try:
        client = Groq(api_key=os.environ.get('GROQ_API_KEY'))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.choices[0].message.content
        return jsonify({"success": True, "answer": answer})
    except Exception as e:
        return jsonify({"error": f"AI failed: {str(e)}"}), 500


@app.route('/generate-paper', methods=['POST'])
def generate_paper():
    title        = request.form.get('title', '')
    abstract     = request.form.get('abstract', '')
    paper_format = request.form.get('format', 'IEEE')

    reference_texts = []
    for key in sorted(request.files.keys()):
        if key.startswith('reference_') and len(reference_texts) < 5:
            try:
                pdf_bytes = request.files[key].read()
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                text = "".join(page.get_text() for page in doc)
                doc.close()
                reference_texts.append(clean_text(text)[:3000])
            except Exception as e:
                print(f"Failed to read reference: {e}")

    refs_summary = "\n\n".join(
        f"Reference {i+1}:\n{t}" for i, t in enumerate(reference_texts)
    ) if reference_texts else "No references provided."

    # ── Strict format rules ───────────────────────────────────
    format_rules = {

        "IEEE": """STRICT IEEE JOURNAL FORMAT — FOLLOW EXACTLY AS SHOWN IN REAL IEEE PAPERS:

PAPER STRUCTURE (write every section in this exact order):

1. ABSTRACT
- Start with exactly: Abstract—  (em dash immediately after the word, no space before it)
- Single paragraph, 150-250 words, NO citations, NO sub-points, NO line breaks inside
- Example: "Abstract—This paper presents a novel approach to..."

2. INDEX TERMS
- Immediately after abstract on its own line
- Format exactly: Index Terms—keyword one, keyword two, keyword three, keyword four.

3. SECTION I — INTRODUCTION
- Heading format EXACTLY: I. INTRODUCTION  (Roman numeral, period, space, ALL CAPS)
- Body: full academic paragraphs only — NEVER use bullet points anywhere in the paper body
- Minimum 3 paragraphs covering: background, problem statement, contributions of this paper

4. SECTION II — RELATED WORK
- Heading EXACTLY: II. RELATED WORK
- Cite references inline as [1], [2], [3] — use square brackets always
- Discuss prior work in flowing prose paragraphs, no lists

5. SECTION III — METHODOLOGY
- Heading EXACTLY: III. METHODOLOGY
- Subsections use format: A. Subsection Name  (letter, period, space, Title Case)
- Include numbered equations where relevant: place (1), (2) at the right side
- Example subsection heading: A. Data Preprocessing

6. SECTION IV — RESULTS AND DISCUSSION
- Heading EXACTLY: IV. RESULTS AND DISCUSSION
- Reference figures as: Fig. 1, Fig. 2
- Reference tables as: Table I, Table II
- Add figure placeholders as: [DIAGRAM_HERE: detailed description of figure]
- Compare your results with prior work using reference citations [1], [2]

7. SECTION V — CONCLUSION
- Heading EXACTLY: V. CONCLUSION
- 2-3 paragraphs summarizing findings and future work
- No new claims, no new citations

8. REFERENCES
- Heading EXACTLY: REFERENCES  (ALL CAPS, no Roman numeral)
- Each reference on its own numbered line
- Journal format: [1] A. B. Author and C. D. Author, "Title of paper," Journal Name, vol. X, no. Y, pp. ZZ–ZZ, Mon. Year.
- Book format: [2] A. B. Author, Title of Book. City: Publisher, Year, pp. ZZ–ZZ.
- Conference format: [3] A. B. Author, "Paper title," in Proc. Conf. Name, City, Year, pp. ZZ–ZZ.
- Number references in order of first citation in the text

MANDATORY STYLE RULES — NEVER VIOLATE THESE:
✗ NEVER use bullet points or numbered lists anywhere in the body text
✗ NEVER write "Introduction:" or "1. Introduction" — always use "I. INTRODUCTION"
✗ NEVER write "Abstract:" — always "Abstract—" with em dash (—)
✗ NEVER use markdown bold (**text**) or italic (*text*) or headers (## text)
✓ ALL main section headings must be ALL CAPS with Roman numerals: I. II. III. IV. V.
✓ Subsection headings: A. Name Of Subsection (title case, NOT all caps)
✓ All in-text citations must use square brackets: [1], [2], [1]–[3]
✓ Write minimum 2500 words total
✓ Write in third person academic voice throughout
✓ Every paragraph must be at least 3 full sentences""",

        "APA": """STRICT APA 7th EDITION FORMAT — FOLLOW EXACTLY:

PAPER STRUCTURE:

1. ABSTRACT
- Label: Abstract (bold, centered on its own line)
- Single paragraph, 150-250 words
- Next line: Keywords: word1, word2, word3, word4

2. INTRODUCTION (Level 1 heading — bold, centered, title case — NO number label)
- At least 3 paragraphs of background and rationale
- In-text citations: (Author, Year) or Author (Year) — NEVER [1] brackets

3. LITERATURE REVIEW (Level 1 heading)
- Flowing prose paragraphs citing prior work: (Smith & Jones, 2020)

4. METHODOLOGY (Level 1 heading)
- Level 2 subheadings: left-aligned, bold, title case — e.g., Data Collection
- Level 3 subheadings: indented, bold, italic, sentence case, ends with period.

5. RESULTS (Level 1 heading)

6. DISCUSSION (Level 1 heading)

7. CONCLUSION (Level 1 heading)

8. REFERENCES (Level 1 heading, bold, centered)
- Hanging indent, alphabetical by first author last name
- Journal: Author, A. B., & Author, C. D. (Year). Title in sentence case. Journal Name, volume(issue), pages. https://doi.org/xxxxx
- Book: Author, A. B. (Year). Title in sentence case. Publisher.

MANDATORY STYLE RULES:
✗ NEVER use [1] bracket citations — always author-year: (Author, Year)
✗ NEVER use bullet points in body text
✓ All Level 1 headings: bold, centered, title case
✓ Minimum 2500 words, third person academic voice""",

        "ACM": """STRICT ACM FORMAT — FOLLOW EXACTLY:

PAPER STRUCTURE:

1. ABSTRACT
- Label: ABSTRACT (all caps, bold)
- 150 words maximum, single paragraph
- Followed by: CCS CONCEPTS
- Format: • Computing methodologies → Machine learning; • Applied computing → Economics;
- Then: KEYWORDS (all caps)
- Format: keyword1, keyword2, keyword3

2. NUMBERED SECTIONS (ALL CAPS headings):
1. INTRODUCTION
2. RELATED WORK
3. METHODOLOGY
   3.1 Subsection Name
   3.2 Another Subsection
4. RESULTS
5. DISCUSSION
6. CONCLUSION
REFERENCES

- Section format: number. SECTION NAME  (number period space ALL CAPS)
- Subsection format: number.number Subsection Name  (title case)

CITATION STYLE:
- Inline: [1], [2, 3], [4–6]  — always square brackets
- References numbered in order of first appearance

REFERENCE FORMAT:
[1] Author, A. B., and Author, C. D. Year. Title of paper. In Proceedings of Conference Name (City, Country, Date), ACM, Pages. https://doi.org/xxx
[2] Author, A. B. Year. Book Title. Publisher.

MANDATORY STYLE RULES:
✗ NEVER use bullet points in body text — all prose paragraphs
✓ Minimum 2500 words, technical academic voice""",

        "MLA": """STRICT MLA 9th EDITION FORMAT — FOLLOW EXACTLY:

PAPER STRUCTURE:
- Header block top left (each on own line): Author Name / Course Name / Instructor / Date
- Title: centered, title case, NOT bold, NOT underlined
- No separate title page

SECTION HEADINGS (centered, bold, title case, no numbers):
Introduction
Literature Review
Methodology
Results
Discussion
Conclusion
Works Cited

CITATION STYLE:
- In-text: (Author page#) — example: (Smith 45) or (Jones and Brown 102–103)
- For no page number: (Author) — example: (Williams)
- Never use [1] brackets

WORKS CITED FORMAT (at end, alphabetical):
- Book: Author Last, First. Title of Book. Publisher, Year.
- Article: Author Last, First. "Title of Article." Journal Name, vol. X, no. Y, Year, pp. ZZ–ZZ.
- Web: Author Last, First. "Title of Page." Website Name, Day Mon. Year, URL.

MANDATORY STYLE RULES:
✗ NEVER use [1] citations — always (Author page)
✗ NEVER use bullet points in body text
✓ Present tense when discussing sources
✓ Minimum 2500 words, formal academic voice""",

        "Nature": """STRICT NATURE JOURNAL FORMAT — FOLLOW EXACTLY:

PAPER STRUCTURE:

1. ABSTRACT (no label — begin paragraph directly)
- 150 words maximum
- Single paragraph: background sentence, gap sentence, what you did, main finding, implication
- No citations, no sub-headings inside abstract

2. Introduction (heading — title case, not bold, not numbered)
- 4-5 paragraphs establishing context and research gap
- Superscript citations: evidence shows¹ or results agree²,³  — use superscript numbers NOT [1]

3. Results (heading)
- Present findings directly, no interpretation yet
- Subheadings if needed: bold, sentence case, not numbered
- Figure placeholders: [DIAGRAM_HERE: description of figure and what it shows]

4. Discussion (heading)
- Interpret results in context of literature
- Address limitations honestly

5. Methods (heading — placed here at end before references)
- Detailed enough for reproducibility
- Subheadings for each component: bold, sentence case

6. Data Availability (heading)
- One sentence statement

7. References (heading)
- Numbered in order of appearance in text
- Format: Author, A. B., Author, C. D. & Author, E. F. Title of article in sentence case. Journal Abbrev. volume, start–end (year).
- Example: Chen, Y. et al. Financial trading strategy system. Math. Probl. Eng. 2020, 1–13 (2020).

MANDATORY STYLE RULES:
✗ NEVER use [1] bracket citations — use superscript numbers¹
✗ NEVER use bullet points in body text
✗ NEVER number sections
✓ Past tense for Methods and Results
✓ Concise, precise scientific language
✓ Minimum 2500 words""",

        "Springer": """STRICT SPRINGER JOURNAL FORMAT — FOLLOW EXACTLY:

PAPER STRUCTURE:

1. Abstract
- Label: Abstract (bold, on its own line)
- 150-250 words, single paragraph
- Next line: Keywords: word1 · word2 · word3  (use middle dot · as separator)

2. NUMBERED SECTIONS (number space Title Case — NO period after number):
1 Introduction
2 Related Work
  2.1 First Subtopic
  2.2 Second Subtopic
3 Methodology
  3.1 Data Collection
  3.2 Model Design
4 Results
5 Discussion
6 Conclusion
References

- Main section: just the number and title — e.g.:  3 Methodology
- Subsection: number.number and title — e.g.:  3.1 Data Collection
- Sub-subsection: number.number.number — e.g.:  3.1.1 Feature Engineering

CITATION STYLE:
- Inline: [1], [2, 3], [4–6]  — square brackets, numbered in order of appearance

REFERENCE FORMAT (numbered list, no brackets around number):
1. Author, A.B., Author, C.D.: Title of paper. Journal Name 45(3), 123–145 (2020). https://doi.org/10.1000/xyz
2. Author, A.B.: Book Title, pp. 45–67. Publisher, City (Year)
3. Author, A.B., Author, C.D.: Paper title. In: Editor, A. (ed.) Conference Name, pp. 45–67. Springer, Heidelberg (2020)

MANDATORY STYLE RULES:
✗ NEVER use bullet points in body text — all flowing prose paragraphs
✗ NEVER put a period after the section number: write "3 Methodology" NOT "3. Methodology"
✓ Minimum 2500 words
✓ Formal academic language, third person voice"""
    }

    rules = format_rules.get(paper_format, format_rules["IEEE"])

    prompt = f"""You are an expert academic paper writer. Write a complete, professional, publication-ready research paper in {paper_format} format.

Paper Title: {title}

Abstract provided by author:
{abstract}

Reference papers to cite (extract key ideas and cite them):
{refs_summary}

{rules}

WRITE THE COMPLETE PAPER NOW IN THIS EXACT ORDER:
1. Abstract
2. Index Terms / Keywords
3. All main sections (Introduction through Conclusion)
4. References list

GENERAL REQUIREMENTS (apply to all formats):
- Minimum 2500 words total
- Write detailed, flowing academic paragraphs — NEVER use bullet points in the body text
- Cite the provided references throughout the paper using the correct citation style for {paper_format}
- Insert figure placeholders [DIAGRAM_HERE: description] where relevant diagrams would appear
- Use third person academic voice throughout
- Methodology section must include relevant equations or mathematical formulations
- Results section must include a comparison analysis referencing prior work
- Do NOT include any markdown formatting symbols like **, *, or ##

Begin writing the paper now:"""

    try:
        client = Groq(api_key=os.environ.get('GROQ_API_KEY'))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=6000,
            messages=[{"role": "user", "content": prompt}]
        )
        paper_content = response.choices[0].message.content
    except Exception as e:
        return jsonify({"error": f"AI generation failed: {str(e)}"}), 500

    # ── Build .docx ───────────────────────────────────────────
    try:
        doc = Document()

        for sec in doc.sections:
            sec.top_margin    = Cm(2.5)
            sec.bottom_margin = Cm(2.5)
            sec.left_margin   = Cm(1.5)
            sec.right_margin  = Cm(1.5)
            sec.page_width    = Cm(21.59)
            sec.page_height   = Cm(27.94)

        lines = paper_content.split('\n')

        # ── Parse sections ────────────────────────────────────
        abstract_lines = []
        keyword_lines  = []
        body_lines     = []
        mode = 'before'

        body_triggers = [
            'introduction', 'related', 'i.', 'ii.', '1.', '2.',
            '1 ', '2 ', 'methodology', 'results', 'conclusion'
        ]
        body_headings = [
            'introduction', 'related work', 'literature review',
            'methodology', 'proposed', 'method', 'approach',
            'results', 'discussion', 'experiment', 'evaluation',
            'conclusion', 'future work', 'references', 'acknowledgment',
            'data availability', 'conflicts of interest'
        ]

        for raw_line in lines:
            s = raw_line.strip()
            if not s:
                continue

            lower = s.lower()
            # Strip Roman numerals and numbers for comparison
            clean = re.sub(r'^[IVXivx]+\.\s*|^\d+\.?\s*', '', lower).strip().rstrip('.')

            # Detect abstract start
            if re.match(r'^abstract[—\-:]*\s*', lower):
                mode = 'abstract'
                after = re.sub(r'^abstract[—\-:]*\s*', '', s, flags=re.IGNORECASE).strip()
                if after:
                    abstract_lines.append(after)
                continue

            # Detect keywords/index terms
            if re.match(r'^(keywords?|index terms?)[—\-:]*', lower):
                mode = 'keywords'
                keyword_lines.append(s)
                continue

            if mode == 'abstract':
                is_body_start = (
                    any(clean.startswith(k) for k in body_triggers) and len(s) < 90
                ) or is_ieee_section_heading(s)
                if is_body_start:
                    mode = 'body'
                    body_lines.append(s)
                else:
                    abstract_lines.append(s)

            elif mode == 'keywords':
                is_body_start = (
                    any(clean.startswith(k) for k in body_triggers) and len(s) < 90
                ) or is_ieee_section_heading(s)
                if is_body_start:
                    mode = 'body'
                    body_lines.append(s)
                else:
                    keyword_lines.append(s)

            elif mode == 'body':
                body_lines.append(s)

            else:
                # 'before' mode — check if we should start abstract
                if re.match(r'^abstract', lower):
                    mode = 'abstract'

        # ── Write title ───────────────────────────────────────
        tp = doc.add_paragraph()
        tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        tp.paragraph_format.space_after = Pt(6)
        tr = tp.add_run(title)
        tr.bold = True
        tr.font.name = 'Times New Roman'
        tr.font.size = Pt(16)

        # Format badge
        fp = doc.add_paragraph()
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fp.paragraph_format.space_after = Pt(12)
        fr = fp.add_run(f"[ {paper_format} Format ]")
        fr.italic = True
        fr.font.size = Pt(9)
        fr.font.name = 'Times New Roman'

        # ── Abstract heading ──────────────────────────────────
        ah = doc.add_paragraph()
        ah.alignment = WD_ALIGN_PARAGRAPH.LEFT
        ah.paragraph_format.space_after = Pt(2)

        if paper_format == "IEEE":
            ahr = ah.add_run("Abstract")
            ahr.bold = True
            ahr.font.size = Pt(9)
            ahr.font.name = 'Times New Roman'
            ahr_dash = ah.add_run("—")
            ahr_dash.bold = True
            ahr_dash.font.size = Pt(9)
            ahr_dash.font.name = 'Times New Roman'
        else:
            ahr = ah.add_run("Abstract")
            ahr.bold = True
            ahr.font.size = Pt(10)
            ahr.font.name = 'Times New Roman'

        # ── Abstract body ─────────────────────────────────────
        abs_text = ' '.join(abstract_lines).strip()
        abs_text = re.sub(r'^abstract[—\-:]*\s*', '', abs_text, flags=re.IGNORECASE).strip()
        abs_text = re.sub(r'\s+', ' ', abs_text)
        if not abs_text:
            abs_text = abstract

        if paper_format == "IEEE":
            # Append abstract text to the same paragraph as "Abstract—"
            apr = ah.add_run(abs_text)
            apr.font.size = Pt(9)
            apr.font.name = 'Times New Roman'
            ah.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            ah.paragraph_format.space_after = Pt(6)
        else:
            ap = doc.add_paragraph()
            ap.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            ap.paragraph_format.space_after = Pt(6)
            apr = ap.add_run(abs_text)
            apr.font.size = Pt(10)
            apr.font.name = 'Times New Roman'
            apr.italic = True

        # ── Keywords / Index Terms ────────────────────────────
        kw_text = ' '.join(keyword_lines).strip()
        kw_text = re.sub(r'\s+', ' ', kw_text)
        if kw_text:
            kp = doc.add_paragraph()
            kp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            kp.paragraph_format.space_after = Pt(10)
            kpr = kp.add_run(kw_text)
            kpr.italic = True
            kpr.font.size = Pt(9)
            kpr.font.name = 'Times New Roman'

        # ── Section break → two columns ───────────────────────
        insert_continuous_section_break(doc, two_col=True)

        # ── Body lines ────────────────────────────────────────
        for raw in body_lines:
            line = clean_line(raw)
            if not line:
                continue

            # IEEE main section heading: I. INTRODUCTION
            if is_ieee_section_heading(line):
                h = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(10)
                h.paragraph_format.space_after  = Pt(4)
                hr = h.add_run(line.upper())
                hr.bold = True
                hr.font.size = Pt(10)
                hr.font.name = 'Times New Roman'

            # IEEE subsection: A. Name
            elif paper_format == "IEEE" and is_ieee_subsection_heading(line):
                h = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(6)
                h.paragraph_format.space_after  = Pt(3)
                hr = h.add_run(line)
                hr.italic = True
                hr.bold   = True
                hr.font.size = Pt(10)
                hr.font.name = 'Times New Roman'

            # Generic heading for all other formats
            elif is_generic_section_heading(line, body_headings):
                h = doc.add_paragraph()
                h.alignment = WD_ALIGN_PARAGRAPH.LEFT
                h.paragraph_format.space_before = Pt(10)
                h.paragraph_format.space_after  = Pt(4)
                # For IEEE force uppercase, others keep original
                text_to_write = line.upper() if paper_format == "IEEE" else line
                hr = h.add_run(text_to_write)
                hr.bold = True
                hr.font.size = Pt(10)
                hr.font.name = 'Times New Roman'

            # Figure placeholder
            elif is_figure_placeholder(line):
                fp2 = doc.add_paragraph()
                fp2.alignment = WD_ALIGN_PARAGRAPH.CENTER
                fp2.paragraph_format.space_before = Pt(8)
                fp2.paragraph_format.space_after  = Pt(8)
                fr2 = fp2.add_run(f'[ Figure: {line} ]')
                fr2.bold = True
                fr2.italic = True
                fr2.font.size = Pt(8)
                fr2.font.name = 'Times New Roman'

            # Reference line [1] ...
            elif is_reference_line(line):
                rp = doc.add_paragraph()
                rp.paragraph_format.left_indent       = Inches(0.3)
                rp.paragraph_format.first_line_indent = Inches(-0.3)
                rp.paragraph_format.space_after       = Pt(2)
                rr = rp.add_run(line)
                rr.font.size = Pt(8)
                rr.font.name = 'Times New Roman'

            # Keyword line appearing in body (sometimes model repeats them)
            elif re.match(r'^(keywords?|index terms?)', line.lower()):
                kp2 = doc.add_paragraph()
                kp2.paragraph_format.space_after = Pt(6)
                kr2 = kp2.add_run(line)
                kr2.italic = True
                kr2.font.size = Pt(9)
                kr2.font.name = 'Times New Roman'

            # Normal body paragraph
            else:
                pp = doc.add_paragraph()
                pp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                pp.paragraph_format.first_line_indent = Inches(0.2)
                pp.paragraph_format.space_after       = Pt(4)
                pp.paragraph_format.line_spacing      = Pt(11)
                ppr = pp.add_run(line)
                ppr.font.size = Pt(10)
                ppr.font.name = 'Times New Roman'

        set_two_columns(doc.sections[-1])

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        doc_b64 = base64.b64encode(buf.read()).decode('utf-8')

        return jsonify({
            "success":    True,
            "content":    paper_content,
            "docx_base64": doc_b64,
            "filename":   f"{title[:50].replace(' ', '_')}_{paper_format}.docx"
        })

    except Exception as e:
        return jsonify({"error": f"Document generation failed: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(debug=False, port=8080)