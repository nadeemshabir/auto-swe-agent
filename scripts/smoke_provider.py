import os
import sys
from dotenv import load_dotenv

# Load the .env file from the current directory
load_dotenv(".env")

from agent.providers import get_provider
from agent.providers.base import ProviderError

def main():
    print(f"Loaded Provider: {os.getenv('LLM_PROVIDER')}")
    print(f"Loaded Model: {os.getenv('LLM_MODEL')}")
    
    try:
        provider = get_provider()
        print("\nSending 'Hello, are you online?' to the model...")
        
        # Build the message using the provider's native format builder
        msg = provider.user_message("Hello, are you online? Respond with exactly one word: 'Yes'.")
        
        # Call the API using the correct interface
        response = provider.complete(
            system="You are a helpful assistant.",
            messages=[msg],
            tools=[],
            max_tokens=50
        )
        
        print("\n--- RESPONSE ---")
        print(response.text)
        print("----------------")
        print(f"Cost: ${provider.cost_usd(response.usage):.5f}")
        
    except ProviderError as e:
        print(f"\nProvider Error: {e}")
    except Exception as e:
        print(f"\nUnexpected Error: {e}")

if __name__ == "__main__":
    main()
