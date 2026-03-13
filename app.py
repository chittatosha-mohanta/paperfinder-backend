from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
import pdfplumber
import io
import re

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173", "http://localhost:5174", "https://paperfinder-pro.vercel.app"])

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
        if len(w) > 4
        and w.isalpha()
        and w.lower() not in stop_words
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

if __name__ == '__main__':
    app.run(debug=True, port=8080)