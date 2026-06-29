import os
import json
import logging
from google import genai
from google.genai import types
from app.schema.models import RMCDataEnhanced

logger = logging.getLogger(__name__)


def get_gemini_client():
    """Initializes the GenAI client using GEMINI_API_KEY from the environment."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning(
            "GEMINI_API_KEY environment variable is not set. Gemini features will be disabled.")
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {str(e)}")
        return None


def extract_rmc_with_gemini(full_text: str, rule_based_data: dict,
                            few_shot_context: str = "") -> RMCDataEnhanced:
    """
    Extractor Agent (Phase 3 & 4):
    Uses Gemini 2.5 Flash with structured output schema (RMCDataEnhanced) to extract
    grounded parameter values, exact source quotes, and step-by-step reasoning.
    Accepts optional few-shot context from past validated quotations.
    """
    client = get_gemini_client()
    if not client:
        logger.warning(
            "Gemini Client not available. Creating mock grounded response from rule-based results.")
        enhanced_dict = {}
        for key in RMCDataEnhanced.model_fields.keys():
            if key in ["mandatory_spares", "om_spares"]:
                enhanced_dict[key] = rule_based_data.get(key, [])
            else:
                val = rule_based_data.get(key, "XX")
                enhanced_dict[key] = {
                    "value": val,
                    "quote": "Extracted via rule-based regex patterns" if val != "XX" else "Not Found",
                    "reasoning": "Fallback rule-based regex extraction engine."
                }
        return RMCDataEnhanced(**enhanced_dict)

    few_shot_prompt = ""
    if few_shot_context:
        few_shot_prompt = f"""
        Here is a relevant example of a past RFQ and its approved RMC extraction to guide your layout understanding:
        ---
        {few_shot_context}
        ---
        """

    prompt = f"""
    You are the primary Extractor Agent. Your job is to extract technical specifications
    and spares tables from industrial tender documents and return a structured JSON matching RMCDataEnhanced.

    {few_shot_prompt}

    Rule-based extraction reference (some fields might be missing or 'XX'):
    {json.dumps(rule_based_data, indent=2)}

    For each parameter:
    - Extract the value (e.g. "50 LPM", "SS304", "PLC Based"). If not found, use "XX".
    - Cite the EXACT TEXT QUOTE (substring) from the document text justifying this extraction.
    - Provide your step-by-step reasoning explaining why you extracted this value and how you resolved any contradictions.

    Tender Document Text:
    ---
    {full_text[:35000]}
    ---
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RMCDataEnhanced,
                temperature=0.1,
            ),
        )
        if response.text:
            return RMCDataEnhanced.model_validate_json(response.text)
    except Exception as e:
        logger.error(
            f"Gemini Structured Extraction failed: {
                str(e)}. Falling back.")
        err_msg = str(e)
        # Fallback structure (built inside except so err_msg is in scope)
        enhanced_dict = {}
        for key in RMCDataEnhanced.model_fields.keys():
            if key in ["mandatory_spares", "om_spares"]:
                enhanced_dict[key] = rule_based_data.get(key, [])
            else:
                val = rule_based_data.get(key, "XX")
                enhanced_dict[key] = {
                    "value": val,
                    "quote": "Rule-based fallback due to API error",
                    "reasoning": f"Gemini API error: {err_msg}. Using baseline parser."
                }
        return RMCDataEnhanced(**enhanced_dict)


def audit_rmc_with_critic(
        full_text: str, initial_rmc: RMCDataEnhanced) -> RMCDataEnhanced:
    """
    Auditor Agent (Phase 9):
    Runs a separate critic pass using Gemini 2.5 Flash. It reviews the initial extraction,
    compares quotes and values against source text, resolves contradictions, and outputs a audited JSON.
    """
    client = get_gemini_client()
    if not client:
        logger.warning(
            "Gemini Client not available. Skipping Critic Auditing loop.")
        return initial_rmc

    # Serialize initial extraction
    initial_json = initial_rmc.model_dump_json(indent=2)

    prompt = f"""
    You are the Critic-Auditor Agent. Your job is to audit and refine the initial extraction RMC JSON
    provided below against the raw tender text to ensure absolute accuracy.

    Initial Extraction JSON (to audit):
    {initial_json}

    Tender Document Text:
    ---
    {full_text[:35000]}
    ---

    AUDITING CRITERIA:
    1. Verify every parameter's value against the text. Ensure no hallucinations or incorrect assumptions were made.
    2. Check the cited 'quote'. It MUST exist as a literal substring in the document text. If the quote doesn't exist, locate the correct sentence, update the quote, and correct the value if needed.
    3. Resolve contradiction using the prioritization rules: RFQ Email > LOS Spec > General Tech Spec > BOM.
       If you change a value, clearly explain why in the 'reasoning' field (e.g. 'Audited: Overwrote preliminary spec with latest email negotiated value').
    4. Fill in any values currently marked as 'XX' if you find them in the document text.
    5. Audit the spares tables (mandatory_spares, om_spares) for completeness.

    Output the final, audited, and audited JSON matching the RMCDataEnhanced schema.
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RMCDataEnhanced,
                temperature=0.0,  # Zero temperature for highest auditing consistency
            ),
        )
        if response.text:
            logger.info(
                "Critic-Auditor agent successfully reviewed and audited RMC parameters.")
            return RMCDataEnhanced.model_validate_json(response.text)
    except Exception as e:
        logger.error(
            f"Critic-Auditor loop failed: {str(e)}. Using initial extraction.")

    return initial_rmc


def answer_query_with_rag(query: str, context_chunks: list) -> str:
    """
    Answers an engineering query using retrieved document passages as context.
    """
    client = get_gemini_client()
    if not client:
        return "Error: Gemini client is not initialized. Please set the GEMINI_API_KEY environment variable to use Document Chat."

    formatted_context = []
    for idx, chunk in enumerate(context_chunks):
        c_type = chunk.get("type", "text")
        source_label = "Table Source" if c_type == "table" else "Text Source"
        formatted_context.append(
            f"{source_label}: {
                chunk['source_file']} (Page {
                chunk['page_no']})\nContent:\n{
                chunk['text']}"
        )
    context_str = "\n\n---\n\n".join(formatted_context)

    prompt = f"""
    You are an expert engineering assistant analyzing industrial tender specifications.
    Answer the following user query based ONLY on the provided document contexts.
    Cite the page number and file name when answering.

    Query: {query}

    Document Contexts:
    {context_str}

    Response:
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return response.text if response.text else "No response generated."
    except Exception as e:
        logger.error(f"Gemini Query failed: {str(e)}")
        return f"Error communicating with Gemini: {str(e)}"
