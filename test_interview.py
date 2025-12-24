import requests
import json
import time

def test_interview_flow():
    base_url = "http://localhost:5000/api"
    
    # 1. Start the interview
    print("\n=== Starting Interview ===")
    start_data = {
        "context": "Test interview",
        "seed_questions": [
            "What is your experience with Python?",
            "Tell me about a challenging project you've worked on."
        ],
        "mode": "quick"
    }
    
    response = requests.post(f"{base_url}/start", json=start_data)
    print("Start Response:", response.status_code, response.json())
    
    # 2. Answer first main question
    print("\n=== Answering Main Question 1 ===")
    answer1 = {"answer": "I have 3 years of experience with Python, mainly in web development and data analysis."}
    response = requests.post(f"{base_url}/answer", json=answer1)
    print("Answer 1 Response:", response.status_code, response.json())
    
    # 3. Answer first follow-up
    print("\n=== Answering Follow-up 1 ===")
    followup1 = {"answer": "I've used Python with Django for building REST APIs and Flask for smaller applications."}
    response = requests.post(f"{base_url}/answer", json=followup1)
    print("Follow-up 1 Response:", response.status_code, response.json())
    
    # 4. Answer second main question
    print("\n=== Answering Main Question 2 ===")
    answer2 = {"answer": "One challenging project was building a real-time analytics dashboard that processed large datasets."}
    response = requests.post(f"{base_url}/answer", json=answer2)
    print("Answer 2 Response:", response.status_code, response.json())
    
    # 5. Answer second follow-up
    print("\n=== Answering Follow-up 2 ===")
    followup2 = {"answer": "The main challenge was optimizing the data processing pipeline to handle real-time updates efficiently."}
    response = requests.post(f"{base_url}/answer", json=followup2)
    print("Final Response:", response.status_code, response.json())

if __name__ == "__main__":
    test_interview_flow()
