"""
RollCall Manager - Handles in-memory cache + database synchronization
This replaces the chat={} dictionary with proper database-backed storage
"""

from typing import List, Dict, Optional
from models import RollCall, db
from db import create_rollcall

import logging

class RollCallManager:
    """
    Manages rollcalls with in-memory cache and database persistence
    """
    def __init__(self):
        # In-memory cache: {chat_id: {'rollCalls': [RollCall objects], 'shh': bool, 'adminRights': bool}}
        self._cache = {}
    
    def get_chat(self, chat_id: int) -> Dict:
        """Get or create chat data"""
        if chat_id not in self._cache:
            # Load from database (FIXED: removed duplicate call)
            chat_settings = db.get_or_create_chat(chat_id)
            
            # Load active rollcalls from database
            rollcalls_data = db.get_active_rollcalls(chat_id)
            rollcalls = [RollCall(title="", db_id=rc['id']) for rc in rollcalls_data]
            
            self._cache[chat_id] = {
                'rollCalls': rollcalls,
                'shh': chat_settings.get('shh_mode', False),
                'adminRights': chat_settings.get('admin_rights', False),
                'timezone': chat_settings.get('timezone', 'Asia/Calcutta')
            }
        
        return self._cache[chat_id]
    
    def get_rollcalls(self, chat_id: int) -> List[RollCall]:
        """Get all active rollcalls for a chat"""
        chat = self.get_chat(chat_id)
        return chat['rollCalls']
    
    def add_rollcall(self, chat_id: int, title: str) -> RollCall:
        """Create and add a new rollcall"""
        chat = self.get_chat(chat_id)
        
        # Create new rollcall (automatically saves to DB)
        rc = RollCall(title, chat_id=chat_id)
        
        # Add to memory cache
        chat['rollCalls'].append(rc)
        logging.info(f"Created rollcall '{title}' with ID {rc.id} for chat {chat_id}")
        return rc
    
    def remove_rollcall(self, chat_id: int, rc_number: int):
        """Remove a rollcall by index"""
        chat = self.get_chat(chat_id)
        if 0 <= rc_number < len(chat['rollCalls']):
            rc = chat['rollCalls'][rc_number]
            # Mark as inactive in database
            db.end_rollcall(rc.id)
            # Remove from memory cache
            chat['rollCalls'].pop(rc_number)
            logging.info(f"Ended rollcall ID {rc.id} for chat {chat_id}")
        else:
            raise IndexError(f"RollCall index {rc_number} out of range")
    
    def get_rollcall(self, chat_id: int, rc_number: int) -> Optional[RollCall]:
        """Get a specific rollcall by index"""
        chat = self.get_chat(chat_id)
        if 0 <= rc_number < len(chat['rollCalls']):
            return chat['rollCalls'][rc_number]
        return None
    
    def set_shh_mode(self, chat_id: int, enabled: bool):
        """Set shh mode for a chat"""
        chat = self.get_chat(chat_id)
        chat['shh'] = enabled
        # Save to database
        db.update_chat_settings(chat_id, shh_mode=enabled)
    
    def get_shh_mode(self, chat_id: int) -> bool:
        """Get shh mode status"""
        chat = self.get_chat(chat_id)
        return chat.get('shh', False)
    
    def set_admin_rights(self, chat_id: int, enabled: bool):
        """Set admin rights requirement"""
        chat = self.get_chat(chat_id)
        chat['adminRights'] = enabled
        # Save to database
        db.update_chat_settings(chat_id, admin_rights=enabled)
    
    def get_admin_rights(self, chat_id: int) -> bool:
        """Get admin rights status"""
        chat = self.get_chat(chat_id)
        return chat.get('adminRights', False)
    
    def set_timezone(self, chat_id: int, timezone: str):
        """Set timezone for a chat"""
        chat = self.get_chat(chat_id)
        chat['timezone'] = timezone
        
        # Update all active rollcalls
        for rc in chat['rollCalls']:
            rc.timezone = timezone
            rc.save()
        
        # Save to database
        db.update_chat_settings(chat_id, timezone=timezone)
    
    def reload_chat(self, chat_id: int):
        """Reload chat data from database (useful after external changes)"""
        if chat_id in self._cache:
            del self._cache[chat_id]
        return self.get_chat(chat_id)
    
    def clear_cache(self):
        """Clear all cached data (useful for testing)"""
        self._cache.clear()
        logging.info("RollCall cache cleared")

# Global manager instance
manager = RollCallManager()

# Helper functions for backward compatibility with existing code
def get_chat_data(chat_id: int) -> Dict:
    """Get chat data - replaces chat[cid]"""
    return manager.get_chat(chat_id)

def chat_exists(chat_id: int) -> bool:
    """Check if chat has been initialized"""
    return True  # Always true now since we auto-create

def get_rollcalls(chat_id: int) -> List[RollCall]:
    """Get rollcalls list - replaces chat[cid]['rollCalls']"""
    return manager.get_rollcalls(chat_id)
