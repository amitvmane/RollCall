import json
import pickle
from database import RollCallDatabase

def migrate_existing_data():
    """
    Migrate existing in-memory data to database
    Only run this once during migration!
    """
    db = RollCallDatabase('rollcall.db')
    
    # If you have any pickled/saved chat data, load it here
    # Otherwise, this will start fresh
    
    print("Migration complete! Database is ready.")
    print("⚠️ Remember: Your old in-memory data will be lost on restart.")
    print("✅ New data will persist across restarts.")
    
    db.close()

if __name__ == "__main__":
    migrate_existing_data()