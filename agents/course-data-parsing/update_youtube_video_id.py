import pandas as pd
import requests
import urllib.parse
import re
import time

def get_top_youtube_video_id(query):
    """Searches YouTube and returns the first video ID found."""
    # Append 'tutorial' to improve search accuracy for course lessons
    search_query = urllib.parse.quote(f"{query} tutorial")
    url = f"https://www.youtube.com/results?search_query={search_query}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        # Regex to find the video ID in YouTube's raw HTML
        matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', response.text)
        
        if matches:
            return matches[0]
    except Exception as e:
        print(f"Error fetching '{query}': {e}")
        
    return None

def main():
    input_file = 'video_lessons.csv'
    output_file = 'video_lessons_updated.csv'
    
    print(f"Loading {input_file}...")
    df = pd.read_csv(input_file)
    
    print(f"Processing {len(df)} lessons. This will take a few minutes...")
    
    # Iterate through the DataFrame and update missing video IDs
    for index, row in df.iterrows():
        # Only search if the youtube_video_id is currently missing or NaN
        if pd.isna(row['youtube_video_id']) or str(row['youtube_video_id']).strip() == "":
            title = row['title']
            print(f"[{index + 1}/{len(df)}] Searching for: {title}")
            
            video_id = get_top_youtube_video_id(title)
            
            if video_id:
                df.at[index, 'youtube_video_id'] = video_id
                print(f"  -> Found ID: {video_id}")
            else:
                print("  -> No video found.")
            
            # Sleep briefly to avoid being rate-limited by YouTube
            time.sleep(1.5)
            
    print(f"\nSaving updated records to {output_file}...")
    df.to_csv(output_file, index=False)
    print("Process complete!")

if __name__ == "__main__":
    main()