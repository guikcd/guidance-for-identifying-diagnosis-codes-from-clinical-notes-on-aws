import boto3
import json
import re
import os
import logging
from botocore.client import Config
from helpers.bedrock_client import BedrockClient
from helpers.prompt import Prompt

# Set up the logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Function to process patient notes with AWS Comprehend Medical
def process_patient_notes_with_comprehendMedical(patient_notes):
    """
    Process patient's notes with AWS Comprehend Medical to extract ICD-10-CM codes.

    Args:
        patient_notes (str): The patient's notes to be processed.

    Returns:
        list: A list of dictionaries containing the extracted text, category, and ICD-10-CM concepts.
    """
    # Initialize AWS Comprehend Medical client
    comprehend_med = boto3.client('comprehendmedical')

    # Call the infer_icd10_cm method to extract ICD-10-CM codes
    cm_response = comprehend_med.infer_icd10_cm(Text=patient_notes)
    cm_output = []
    cm_threshold = 0.70

    # Process the response entities
    response_entities = cm_response["Entities"]
    for entity in response_entities:
        cm_text = entity["Text"]
        cm_category = entity["Category"]
        cm_icd10concepts = entity["ICD10CMConcepts"]

        cm_icd10concepts_above_threshold_score = []
        inclusion_flag = "N"

        # Filter ICD-10-CM concepts based on the threshold score
        for cm_icd10concept in cm_icd10concepts:
            if cm_icd10concept["Score"] >= cm_threshold:
                inclusion_flag = "Y"
                cm_icd10concepts_above_threshold_score.append({
                    'cm_icd10concept': cm_icd10concept
                })

        # Add the entity to the output list if any ICD-10-CM concept meets the threshold
        if inclusion_flag == "Y":
            cm_output.append({
                'cm_text': cm_text,
                'cm_category': cm_category,
                'cm_icd10concepts_above_threshold_score': cm_icd10concepts_above_threshold_score
            })

    return cm_output

# Function to generate the LLM prompt based on Comprehend Medical output
def process_llm_prompt_based_on_comprehend(patient_notes, cm_output):
    """
    Generate the LLM prompt based on the patient's notes and Comprehend Medical output.

    Args:
        patient_notes (str): The patient's notes.
        cm_output (list): The output from Comprehend Medical.

    Returns:
        str: The LLM prompt.
    """
    prompt = Prompt.LLM_PROMPT

    # Replace placeholders in the prompt with actual values
    prompt = prompt.replace("$patient_notes$", patient_notes)

    cm_output_str = "<comprehend_medical>" + json.dumps(cm_output) + "</comprehend_medical>"
    prompt = prompt.replace("$cm_output_str$", cm_output_str)

    return prompt

# Function to generate the final LLM prompt
def process_llm_final_prompt(patient_notes, extracted_recommendation, kb_output):
    """
    Generate the final LLM prompt based on the patient's notes, extracted recommendation, and knowledge base output.

    Args:
        patient_notes (str): The patient's notes.
        extracted_recommendation (str): The extracted recommendation from the previous LLM response.
        kb_output (list): The output from the knowledge base.

    Returns:
        str: The final LLM prompt.
    """
    prompt = Prompt.FINAL_PROMPT

    # Replace placeholders in the prompt with actual values
    prompt = prompt.replace("$patient_notes$", patient_notes)
    
    initial_recommendation = "<initial_recommendation>" + extracted_recommendation + "</initial_recommendation>"
    prompt = prompt.replace("$initial_recommendation$", initial_recommendation)

    guidelines = "<guidelines>" + json.dumps(kb_output) + "</guidelines>"
    prompt = prompt.replace("$guidelines$", guidelines)

    return prompt

# Function to process the LLM response
def llm_processing(bedrockClient, prompt):
    """
    Call the LLM model and process the response.

    Args:
        bedrockClient (BedrockClient): The Bedrock client instance.
        prompt (str): The prompt for the LLM model.

    Returns:
        str: The LLM model's response.
    """
    # Call the LLM model
    response = bedrockClient.get_bedrock_client().invoke_model(
        modelId=BedrockClient.CLAUDE_3_HAIKU,
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "temperature": 0.0,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
            }
        ),
    )

    # Process and print the response
    result = json.loads(response.get("body").read())
    input_tokens = result["usage"]["input_tokens"]
    output_tokens = result["usage"]["output_tokens"]
    output_list = result.get("content", [])

    logger.info("Invocation details:")
    logger.info(f"- The input length is {input_tokens} tokens.")
    logger.info(f"- The output length is {output_tokens} tokens.")
    logger.info(f"- The model returned {len(output_list)} response(s):")

    llm_output_text = ""
    for output in output_list:
        llm_output_text = output["text"]

    return llm_output_text

# Function to extract medical conditions from the LLM response
def process_output(response):
    """
    Extract medical conditions from the LLM response.

    Args:
        response (str): The LLM response.

    Returns:
        list: A list of medical conditions extracted from the response.
    """
    pattern = r'<recommendation>(.*?)</recommendation>'
    match = re.search(pattern, response, re.DOTALL)

    medical_conditions = []

    if match:
        extracted_recommendation = match.group(1)
        extracted_recommendation_json = json.loads(extracted_recommendation)

        # Extract active conditions
        for item in extracted_recommendation_json["Active_Condition"]:
            medical_conditions.append(item["Review"]["Description"])

        # Extract inactive conditions
        if "Inactive_Condition" in extracted_recommendation_json:
            for item in extracted_recommendation_json["Inactive_Condition"]:
                medical_conditions.append(item["Review"]["Description"])

    else:
        logger.info("No match found.")

    return medical_conditions

# Function to fetch results from the knowledge base
def fetch_results_from_knowledge_base(bedrockClient, medical_conditions):
    """
    Fetch results from the knowledge base for the given medical conditions.

    Args:
        bedrockClient (BedrockClient): The Bedrock client instance.
        medical_conditions (list): A list of medical conditions.

    Returns:
        list: A list of dictionaries containing the medical condition and knowledge base context.
    """
    numberOfResults = int(os.getenv('NumberOfResults'))
    knowledgeBaseId = os.getenv('KnowledgeBaseId')
    kb_output = []

    for medical_condition in medical_conditions:
        query = "Find all the information about " + medical_condition

        # Call the knowledge base API
        kb_response = bedrockClient.get_bedrock_agent_client().retrieve(
            retrievalQuery={
                'text': query
            },
            knowledgeBaseId=knowledgeBaseId,
            retrievalConfiguration={
                'vectorSearchConfiguration': {
                    'numberOfResults': numberOfResults
                }
            }
        )

        kb_output.append({
            'medical_condition': medical_condition,
            'kb_context': kb_response['retrievalResults']
        })

    return kb_output

# Lambda function handler
def lambda_handler(event, context):
    """
    AWS Lambda function handler.

    Args:
        event (dict): The event data from AWS Lambda.
        context (object): The context object from AWS Lambda.

    Returns:
        dict: The response object to be returned by AWS Lambda.
    """
    requestBody = json.loads(event['body'])
    logger.info(f'request : {event}')
    defaultRegion = os.getenv('DefaultRegion')

    patient_notes = event['body']

    # Initialize the Bedrock client
    bedrockClient = BedrockClient(region_name=defaultRegion)

    # Call Comprehend Medical to get ICD-10-CM codes from patient notes
    cm_response = process_patient_notes_with_comprehendMedical(patient_notes)
    logger.info(f'Comprehend Medical response: {cm_response}')

    # Generate the LLM prompt based on Comprehend Medical output
    prompt = process_llm_prompt_based_on_comprehend(patient_notes, cm_response)
    logger.info(f'Prompt with Comprehend Medical response: {prompt}')

    # Call the LLM model
    logger.info('-----Calling LLM with Comprehend Medical results-----')
    response = llm_processing(bedrockClient, prompt)
    logger.info(f'LLM response with Comprehend Medical input: {response}')
    
    # Extract medical conditions from the LLM response
    medical_conditions = process_output(response)
    logger.info(f'Medical conditions: {medical_conditions}')

    # Fetch results from the knowledge base
    kb_output = fetch_results_from_knowledge_base(bedrockClient, medical_conditions)
    logger.info(f'KB output: {kb_output}')

    # Generate the final LLM prompt
    final_prompt = process_llm_final_prompt(patient_notes, response, kb_output)
    logger.info(f'Final prompt: {final_prompt}')

    # Call the LLM model with the final prompt
    logger.info('-----Calling LLM with final prompt-----')
    final_response = llm_processing(bedrockClient, final_prompt)
    logger.info(f'Final LLM response: {final_response}')
    
    return {
        'statusCode': 200,
        'body': json.dumps(final_response)
    }