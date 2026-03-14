from groq import Groq
import os
import base64
from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
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

def clean_text(text):
    text = re.sub(r'[^\x20-\x7E\n]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_query(text):
    stop_words = {
        'the', 'and', 'for', 'with', 'from', 'this', 'that', 'are', 'was',
        'were', 'has', 'have', 'been', 'will', 'can', 'may', 'our', 'their',
        'also', 'which', 'when', 'where', 'how', 'what', 'all', 'not', 'but',
        'its', 'than', 'more', 'some', 'such', 'each', 'both', 'only', 'very',
        'most', 'into', 'over', 'after', 'under', 'about', 'other', 'these',
        'those', 'then', 'them', 'they', 'would', 'could', 'should', 'while',
        'between', 'through', 'during', 'before', 'any', 'use', 'used', 'using',
        'based', 'show', 'shows', 'paper', 'study', 'research', 'article',
        'results', 'result', 'method', 'methods', 'data', 'model', 'models',
        'figure', 'table', 'section', 'university', 'college', 'china', 'shanghai',
        'received', 'accepted', 'published', 'copyright', 'journal', 'volume',
        'correspondence', 'academic', 'address', 'revised', 'april', 'march',
        'october', 'january', 'february', 'june', 'july', 'august', 'september',
        'november', 'december', 'email', 'doi', 'http', 'www', 'page', 'pages',
        'dongdong', 'shuhan', 'meizi', 'xiang', 'kemal', 'polat', 'normal'
    }
    words = text.split()
    keywords = [
        w for w in words
        if len(w) > 4 and w.isalpha() and w.lower() not in stop_words
    ]
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
        page = doc[page_num]
        text += page.get_text() + " "
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
            query = extract_query(text)
            return jsonify({
                "success": True,
                "query": query,
                "method": "pymupdf",
                "preview": text[:200]
            })
    except Exception as e:
        print(f"PyMuPDF failed: {e}")

    try:
        text = extract_with_pdfplumber(pdf_bytes)
        if text and len(text.split()) > 15:
            query = extract_query(text)
            return jsonify({
                "success": True,
                "query": query,
                "method": "pdfplumber",
                "preview": text[:200]
            })
    except Exception as e:
        print(f"pdfplumber failed: {e}")

    return jsonify({"error": "Could not extract text from this PDF."}), 422


@app.route('/generate-paper', methods=['POST'])
def generate_paper():
    title = request.form.get('title', '')
    abstract = request.form.get('abstract', '')
    paper_format = request.form.get('format', 'IEEE')

    # Extract text from reference PDFs
    reference_texts = []
    for key in request.files:
        if key.startswith('reference_'):
            file = request.files[key]
            try:
                pdf_bytes = file.read()
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                text = ""
                for page in doc:
                    text += page.get_text()
                doc.close()
                reference_texts.append(clean_text(text)[:3000])
            except Exception as e:
                print(f"Failed to read reference: {e}")

    # Build references summary
    refs_summary = ""
    if reference_texts:
        refs_summary = "\n\n".join([
            f"Reference {i+1}:\n{t}"
            for i, t in enumerate(reference_texts)
        ])

    # Build prompt
    prompt = f"""You are an expert academic paper writer. Write a complete, high-quality research paper in {paper_format} format.

Paper Title: {title}

Abstract provided by author:
{abstract}

Reference papers provided (use these for citations and context):
{refs_summary if refs_summary else "No references provided - write based on title and abstract."}

Instructions:
1. Write a FULL research paper in {paper_format} format
2. Include these sections: Abstract, Introduction, Related Work, Methodology, Results, Discussion, Conclusion, References
3. Cite the reference papers properly using {paper_format} citation style like [1], [2] etc
4. The paper should be detailed, professional, and academic in tone
5. Add [DIAGRAM_HERE] placeholder where a diagram or figure should be inserted
6. Make it at least 2000 words
7. Format each section with the section name on its own line followed by the content

Write the complete paper now:"""

    # Call Groq API
    try:
        client = Groq(api_key=os.environ.get('GROQ_API_KEY'))
        message = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        paper_content = message.choices[0].message.content
    except Exception as e:
        return jsonify({"error": f"AI generation failed: {str(e)}"}), 500

    # Generate Word document
    try:
        doc = Document()

        title_para = doc.add_heading(title, 0)
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        fmt_para = doc.add_paragraph(f"Format: {paper_format}")
        fmt_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph("")

        sections = paper_content.split('\n')
        section_keywords = [
            'abstract', 'introduction', 'related work', 'methodology',
            'results', 'discussion', 'conclusion', 'references'
        ]

        for line in sections:
            line = line.strip()
            if not line:
                continue
            is_heading = any(line.lower().startswith(kw) for kw in section_keywords)
            if is_heading and len(line) < 60:
                doc.add_heading(line, level=1)
            elif '[DIAGRAM_HERE]' in line:
                p = doc.add_paragraph()
                p.add_run('[Figure: Insert diagram here]').bold = True
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                doc.add_paragraph(line)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        doc_bytes = buf.read()
        doc_b64 = base64.b64encode(doc_bytes).decode('utf-8')

        return jsonify({
            "success": True,
            "content": paper_content,
            "docx_base64": doc_b64,
            "filename": f"{title[:50].replace(' ', '_')}.docx"
        })

    except Exception as e:
        return jsonify({"error": f"Document generation failed: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(debug=True, port=8080)