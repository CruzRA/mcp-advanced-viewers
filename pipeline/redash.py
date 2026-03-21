import sys
import requests
import json
import time
import pandas as pd
import os
from dotenv import load_dotenv
load_dotenv()
import argparse

#helper
def format_time(seconds):
    """Formats a time duration in seconds to HH:MM:SS format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining_seconds = seconds % 60
    return f"{hours:02}:{minutes:02}:{remaining_seconds:06.3f}"
class Redash():
    def __init__(self):
        self.redash_url = 'https://redash.scale.com'



    def process(self,batch_size=100):
        
        start = time.perf_counter()

        # Load environment variables from .env file
        load_dotenv()

        self.api_key = os.getenv('REDASH_API_KEY')

        # Set the parameters with configurable initial offset and batch size
        config = {
        'initial_offset': 0,  # Change this to start from a different offset
        'initial_batch_number':1,
        'batch_size': batch_size,  # Number of records per batch
        'output_directory': self.folder,  # Directory for partial CSV files
        'combine_files': True  # Set to False if you don't want a combined file at the end
        }

        # Initialize variables
        current_offset = config['initial_offset']
        batch_number = config['initial_batch_number']
        total_rows = 0
        dataframes = []

        while True:
            # Set parameters for current batch
            params = self.params

            # Include this parameters in the query to use batches
            if batch_size!=100:
                params['limit']=str(config['batch_size'])
                params['offset']=str(current_offset)
            elif batch_number!=1:
                print("No more data to fetch.")
                break

            print(f"\nFetching batch {batch_number} (offset: {current_offset})...")
            
            # Fetch data for the current batch
            try:
                data = self.get_fresh_query_result(params)
                df_batch = self.process_query_result_to_dataframe(data)

                if df_batch.empty:
                    print("No more data to fetch.")
                    break

                # Save current batch to CSV
                batch_filename = f"{config['output_directory']}/batch_{batch_number:04d}_offset_{current_offset}.csv"
                df_batch.to_csv(batch_filename, index=False)
                print(f"Saved batch {batch_number} with {len(df_batch)} rows to {batch_filename}")

                if config['combine_files']:
                    dataframes.append(df_batch)

                total_rows += len(df_batch)
                
                # Update for next iteration
                current_offset += len(df_batch)
                batch_number += 1

                # If fewer rows than the batch size are returned, we've fetched all data
                if len(df_batch) < config['batch_size']:
                    print("Reached the end of data.")
                    break

            except Exception as e:
                print(f"Error processing batch {batch_number}: {str(e)}")
                break

        # Combine all batches if requested
        if config['combine_files'] and dataframes:
            print("\nCombining all batches into a single file...")
            final_df = pd.concat(dataframes, ignore_index=True)
            final_df.to_csv(self.file_name, index=False)
            print(f"Saved combined dataset with {total_rows} rows to {self.file_name}")

        end = time.perf_counter()
        execution_time = format_time(end - start)
        print(f"\nExecution completed in {execution_time}")
        print(f"Total rows processed: {total_rows}")
        print(f"Total batches: {batch_number - 1}")

    def get_fresh_query_result(self, params):
        """Fetch a fresh query result from Redash."""
        session = requests.Session()
        session.headers.update({'Authorization': f'Key {self.api_key}', 'Content-Type': 'application/json'})

        # Prepare the payload with parameters
        payload = {'max_age': 0, 'parameters': params}

        # Send a request to refresh the query result
        response = session.post(f'{self.redash_url}/api/queries/{self.query_id}/results', data=json.dumps(payload))

        if response.status_code != 200:
            try:
                error_message = response.json()
            except json.JSONDecodeError:
                error_message = response.text
            raise Exception(f"Failed to refresh query: {response.status_code}, {error_message}")

        response_data = response.json()

        # Check if the response contains a 'job' key
        if 'job' in response_data:
            job_id = response_data['job']['id']
        else:
            # If 'job' is not in the response, check if we have a direct result
            if 'query_result' in response_data:
                return response_data
            else:
                raise Exception(f"Unexpected response structure: {response_data}")

        # Poll for query completion
        while True:
            job_response = session.get(f'{self.redash_url}/api/jobs/{job_id}')
            job_status = job_response.json()['job']
            status = job_status['status']

            if status == 3:  # Query completed
                print("Query completed successfully.")
                break
            elif status == 4:  # Query failed
                raise Exception(f"Query execution failed: {job_status.get('error', 'No error message')}")
            else:
                time.sleep(5)

        query_result_id = job_status.get('query_result_id')
        if not query_result_id:
            raise Exception("No query result ID found.")

        # Retrieve the result of the completed query
        result_response = session.get(f'{self.redash_url}/api/query_results/{query_result_id}.json')
        if result_response.status_code == 200:
            return result_response.json()

        raise Exception(f"Failed to retrieve results: {result_response.status_code}")

    def process_query_result_to_dataframe(self,result):
        """Converts query result into a Pandas DataFrame."""
        columns = [col['name'] for col in result['query_result']['data']['columns']]
        rows = result['query_result']['data']['rows']
        return pd.DataFrame(rows, columns=columns)

 

    def process_args(self):
        parser = argparse.ArgumentParser(description="Redash")
        parser.add_argument("--folder", default="outputs/batch_000", help="Folder")
        parser.add_argument("--file_name", default="outputs/batch_000/01_redash.csv", help="File name")
        parser.add_argument("--query_id", default=os.getenv('QUERY_ID'), help="Query ID")
        parser.add_argument("--params", default={}, help="Params")
        parser.add_argument("--batch_size", default=100, help="Batch size")
        self.args = parser.parse_args()
        self.folder = self.args.folder
        self.file_name = self.args.file_name
        self.query_id = self.args.query_id
        self.params = self.args.params
        self.batch_size = self.args.batch_size

if __name__ == '__main__':
    # Check if the correct number of arguments is provided
   
    pipeline= Redash()
    pipeline.process_args()
    pipeline.process(pipeline.batch_size)   