import logging
import asyncio
import telebot
from config import TELEGRAM_TOKEN
from exceptions import *
from functions import *
from datetime import datetime
import db
import re
import traceback

bot = telebot.TeleBot(token=TELEGRAM_TOKEN)

class RollCall:
    """RollCall object with database persistence"""
    
class RollCall:
    def __init__(self, title, chat_id=None, db_id=None):
        """
        Initialize RollCall either from database (db_id) or create new (chat_id + title)
        """
        if db_id:
            # Load existing rollcall from database
            self._load_from_db(db_id)
        else:
            # Create new rollcall
            self.title = title
            self.inList = []
            self.outList = []
            self.maybeList = []
            self.waitList = []
            # map proxy user key -> owner user_id
            self.proxy_owners = {}
            self.allNames = []
            self.inListLimit = None
            self.reminder = None
            self.finalizeDate = None
            self.timezone = "Asia/Calcutta"
            self.location = None
            self.event_fee = None
            self.createdDate = datetime.utcnow()

            # Save to database and get ID
            if chat_id:
                # NEW: use db.create_rollcall and store DB id in self.id
                self.id = db.create_rollcall(chat_id, title, self.timezone)
                self.chat_id = chat_id
            else:
                self.id = None
                self.chat_id = None
    
    def _load_from_db(self, db_id):
        """Load rollcall data from database"""
        data = db.get_rollcall(db_id)
        if not data:
            raise Exception(f"RollCall with ID {db_id} not found")
        
        self.id = db_id
        self.chat_id = data['chat_id']
        self.title = data['title']
        self.timezone = data['timezone']
        self.location = data['location']
        self.event_fee = data['event_fee']
        self.inListLimit = data['in_list_limit']
        self.reminder = data['reminder_hours']
        self.proxy_owners = getattr(self, "proxy_owners", {})

        # Parse datetime from database
        if data['finalize_date']:
            self.finalizeDate = datetime.fromisoformat(data['finalize_date'])
        else:
            self.finalizeDate = None
            
        if data['created_at']:
            self.createdDate = datetime.fromisoformat(data['created_at'])
        else:
            self.createdDate = datetime.utcnow()
        
        # Load users from database
        self._load_users_from_db()
    
    def _load_users_from_db(self):
        """Load all users from database"""
        self.inList = []
        self.outList = []
        self.maybeList = []
        self.waitList = []
        self.allNames = []
        
        # Load regular users
        all_users_data = db.get_all_users(self.id)
        for user_data in all_users_data:
            user = User.__new__(User)
            user.user_id = user_data['user_id']
            user.first_name = user_data['first_name']
            user.name = user_data['first_name']
            user.username = user_data['username']
            user.comment = user_data['comment'] or ''
            
            status = user_data['status']
            if status == 'in':
                self.inList.append(user)
            elif status == 'out':
                self.outList.append(user)
            elif status == 'maybe':
                self.maybeList.append(user)
            elif status == 'waitlist':
                self.waitList.append(user)
            
            self.allNames.append(user)
        
        # Load proxy users
        self.proxy_owners = {}  # reset and rebuild from DB each load
        for status in ['in', 'out', 'maybe', 'waitlist']:
            proxy_users_data = db.get_proxy_users_by_status(self.id, status)
            for proxy_data in proxy_users_data:
                user = User.__new__(User)
                user.user_id = proxy_data['name']  # String ID for proxy users
                user.first_name = proxy_data['name']
                user.name = proxy_data['name']
                user.username = None
                user.comment = proxy_data['comment'] or ''
                
                if status == 'in':
                    self.inList.append(user)
                elif status == 'out':
                    self.outList.append(user)
                elif status == 'maybe':
                    self.maybeList.append(user)
                elif status == 'waitlist':
                    self.waitList.append(user)
                
                self.allNames.append(user)
                # NEW: rebuild proxy owner mapping from DB column
                owner_id = proxy_data.get('proxy_owner_id')
                if owner_id is not None:
                    self.proxy_owners[user.user_id] = owner_id
    
    def save(self):
        """Save current rollcall state to database"""
        if self.id:
            db.update_rollcall(
                self.id,
                title=self.title,
                finalize_date=self.finalizeDate.isoformat() if self.finalizeDate else None,
                reminder_hours=self.reminder,
                timezone=self.timezone,
                location=self.location,
                event_fee=self.event_fee,
                in_list_limit=self.inListLimit,
               # proxy_owners=self.proxy_owners,  # NEW (once DB supports it) - not required now
            )
    
    # RETURN INLIST
    def inListText(self):
        txt = f'In:\n'
        i = 0
        for user in self.inList:
            i += 1
            txt += f"{i}. {user}\n"
        return txt + '\n' if len(self.inList) > 0 else "In:\nNobody\n\n"
    
    # RETURN OUTLIST
    def outListText(self):
        txt = f'Out:\n'
        i = 0
        for user in self.outList:
            i += 1
            txt += f"{i}. {user}\n"
        return txt + '\n' if len(self.outList) > 0 else "Out:\nNobody\n\n"
    
    # RETURN MAYBELIST
    def maybeListText(self):
        txt = f'Maybe:\n'
        i = 0
        for user in self.maybeList:
            i += 1
            txt += f"{i}. {user}\n"
        return txt + '\n' if len(self.maybeList) > 0 else "Maybe:\nNobody\n\n"
    
    # RETURN WAITLIST
    def waitListText(self):
        txt = f'Waiting:\n'
        i = 0
        for user in self.waitList:
            i += 1
            txt += f"{i}. {user}\n"
        return (txt + '\n' if len(self.waitList) > 0 else "Waiting:\nNobody") if self.inListLimit != None else ""
    
    # RETURN ALL THE STATES
    def allList(self):
        try:
            _datetime = self.finalizeDate.strftime('%d-%m-%Y %H:%M')
        except:
            _datetime = 'Yet to decide'
        txt = "Title: " + self.title + '\nID: ' + "__RCID__" + f"\nEvent time: {_datetime} {self.timezone if _datetime != 'Yet to decide' else ''}\nLocation: {self.location if self.location != None else 'Yet to decide'}\n\n" + (self.inListText() if self.inListText() != 'In:\nNobody\n\n' else '') + (self.outListText() if self.outListText() != 'Out:\nNobody\n\n' else '') + (self.maybeListText() if self.maybeListText() != 'Maybe:\nNobody\n\n' else '') + (self.waitListText() if self.waitListText() != 'Waiting:\nNobody' else '') + 'Max limit: ' + ('∞' if self.inListLimit == None else str(self.inListLimit))
        return txt
    
    # RETURN THE FINISH LIST (ONLY IN ERC COMMAND)
    def finishList(self):
        try:
            _datetime = self.finalizeDate.strftime('%d-%m-%Y %H:%M')
        except:
            _datetime = ''
        backslash = '\n'
        txt = "Title: " + self.title + '\nID: ' + '__RCID__' + f"{(backslash + 'Event time: ' + _datetime + ' ' + self.timezone) if _datetime != '' else ''}{(backslash + 'Location:' + self.location) if self.location != None else ''}{(backslash + 'Event Fee: ' + str(self.event_fee)) if self.event_fee != None else backslash * 2 + 'In case of paid event - reach out to organiser for payment contribution'}{(backslash + 'Individual Fee: ' + str((round(int(re.sub(r'[^0-9]', '', self.event_fee)) / len(self.inList), 2)) if len(self.inList) > 0 else '0')) if self.event_fee != None else ''}\n\n" + ("Additional unknown/penalty fees are not included and needs to be handled separately.\n\n" if self.event_fee != None else '') + (self.inListText() if self.inListText() != 'In:\nNobody\n\n' else 'In:\nNobody\n\n') + (self.outListText() if self.outListText() != 'Out:\nNobody\n\n' else 'Out:\nNobody\n\n') + (self.maybeListText() if self.maybeListText() != 'Maybe:\nNobody\n\n' else 'Maybe:\nNobody\n\n') + (self.waitListText() if self.waitListText() != 'Waiting:\nNobody' else '') + 'Max limit: ' + ('∞' if self.inListLimit == None else str(self.inListLimit))
        return txt
    
    # DELETE A USER
    def delete_user(self, name):
        """Delete a user by name from database and memory"""
        try:
            # Delete from database
            if db.delete_user_by_name(self.id, name):
                # Reload from database to sync
                self._load_users_from_db()

                # NEW: clean proxy_owners mapping (proxies use name as user_id)
                try:
                    if hasattr(self, "proxy_owners") and self.proxy_owners is not None:
                        if name in self.proxy_owners:
                            del self.proxy_owners[name]
                except Exception:
                    print(traceback.format_exc())

                return True

            return False

        except:
            print(traceback.format_exc())
            return False

    # ADD A NEW USER TO IN LIST
    def addIn(self, user):
        print(self.allNames)
        
        # Check if this is a proxy command trying to move a real user
        if type(user.user_id) == str:
            # Find if a real user with this name exists
            real_user_found = None
            for us in self.allNames:
                if (us.name == user.name or us.first_name == user.name) and type(us.user_id) == int:
                    # Real user found! Use their object instead
                    real_user_found = us
                    break
            
            # If real user exists, use them instead of creating proxy
            if real_user_found:
                user = real_user_found
                # Preserve any new comment from the proxy command
                # (comment is already set in telegram_helper.py)
            else:
                # Only check for duplicate proxy if NO real user exists
                for us in self.allNames:
                    if user.name == us.name and user.user_id != us.user_id:
                        return 'AA'
                
                for us in self.allNames:
                    if us.first_name == user.first_name and us.username == user.username and us.user_id != user.user_id:
                        return "AB"
        
        # ERROR FOR DUPLICATE USER IN THE SAME STATE
        if self.inListLimit == None:
            for us in self.inList:
                if us.user_id == user.user_id and us.comment == user.comment:
                    return "AB"
                elif us.first_name == user.first_name and us.username == user.username and us.user_id != user.user_id:
                    return "AB"
                elif us.user_id == user.user_id and us.comment != user.comment:
                    us.comment = user.comment
                    # Update in database
                    self._save_user_to_db(user, 'in')
                    return
        else:
            for us in self.inList:
                if us.user_id == user.user_id and us.comment == user.comment:
                    return "AB"
                elif us.first_name == user.first_name and us.username == user.username and us.user_id != user.user_id:
                    return "AB"
                elif us.user_id == user.user_id and us.comment != user.comment:
                    us.comment = user.comment
                    self._save_user_to_db(user, 'in')
                    return
                    
            for us in self.waitList:
                if us.user_id == user.user_id and us.comment == user.comment:
                    return "AB"
                elif us.user_id == user.user_id and us.comment != user.comment:
                    us.comment = user.comment
                    self._save_user_to_db(user, 'waitlist')
                    return
        
        # REMOVE THE USER FROM OTHER STATES
        for us in self.outList[:]:
            if us.user_id == user.user_id:
                self.outList.remove(us)
                if us in self.allNames:
                    self.allNames.remove(us)
                    
        for us in self.maybeList[:]:
            if us.user_id == user.user_id:
                self.maybeList.remove(us)
                if us in self.allNames:
                    self.allNames.remove(us)
                    
        for us in self.waitList[:]:
            if us.user_id == user.user_id:
                self.waitList.remove(us)
                if us in self.allNames:
                    self.allNames.remove(us)
        
        # WAITLIST FEATURE
        if self.inListLimit != None:
            if len(self.inList) >= int(self.inListLimit):
                self.waitList.append(user)
                if user not in self.allNames:
                    self.allNames.append(user)
                self._save_user_to_db(user, 'waitlist')
                logging.info(f"The user {user.name} has been added to the Wait list")
                return 'AC'
        
        # ADD THE USER TO THE STATE
        self.inList.append(user)
        if user not in self.allNames:
            self.allNames.append(user)
        self._save_user_to_db(user, 'in')
        logging.info(f"User {user.name} has change his state to in")
    
    # ADD A NEW USER TO OUT LIST
    def addOut(self, user):
        print(self.allNames)
        
        # Check if this is a proxy command trying to move a real user
        if type(user.user_id) == str:
            # Find if a real user with this name exists
            real_user_found = None
            for us in self.allNames:
                if (us.name == user.name or us.first_name == user.name) and type(us.user_id) == int:
                    # Real user found! Use their object instead
                    real_user_found = us
                    break
            
            # If real user exists, use them instead of creating proxy
            if real_user_found:
                user = real_user_found
                # Preserve any new comment from the proxy command
            else:
                # Only check for duplicate proxy if NO real user exists
                for us in self.allNames:
                    if user.name == us.name and user.user_id != us.user_id:
                        return 'AA'
                
                for us in self.allNames:
                    if us.first_name == user.first_name and us.username == user.username and us.user_id != user.user_id:
                        return "AB"
        
        # ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in self.outList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return 'AB'
            elif us.first_name == user.first_name and us.username == user.username and us.user_id != user.user_id:
                return "AB"
            elif us.user_id == user.user_id and us.comment != user.comment:
                us.comment = user.comment
                self._save_user_to_db(user, 'out')
                return
        
        # REMOVE FROM OTHER STATES
        for us in self.inList[:]:
            if us.user_id == user.user_id:
                self.inList.remove(us)
                if us in self.allNames:
                    self.allNames.remove(us)
                    
        for us in self.maybeList[:]:
            if us.user_id == user.user_id:
                self.maybeList.remove(us)
                if us in self.allNames:
                    self.allNames.remove(us)
        
        if self.inListLimit != None:
            for us in self.waitList[:]:
                if us.user_id == user.user_id:
                    self.waitList.remove(us)
                    if us in self.allNames:
                        self.allNames.remove(us)
                        
            if len(self.inList) < int(self.inListLimit) and len(self.waitList) > 0:
                result = self.waitList[0]
                self.inList.append(self.waitList[0])
                self.waitList.pop(0)
                self.outList.append(user)
                if user not in self.allNames:
                    self.allNames.append(user)
                self._save_user_to_db(result, 'in')
                self._save_user_to_db(user, 'out')
                logging.info(f"User {user.name} has change his state to out")
                return result
        
        self.outList.append(user)
        if user not in self.allNames:
            self.allNames.append(user)
        self._save_user_to_db(user, 'out')
        logging.info(f"User {user.name} has change his state to out")
    
    # ADD A NEW USER TO MAYBE LIST
    def addMaybe(self, user):
        print(self.allNames)
        
        # Check if this is a proxy command trying to move a real user
        if type(user.user_id) == str:
            # Find if a real user with this name exists
            real_user_found = None
            for us in self.allNames:
                if (us.name == user.name or us.first_name == user.name) and type(us.user_id) == int:
                    # Real user found! Use their object instead
                    real_user_found = us
                    break
            
            # If real user exists, use them instead of creating proxy
            if real_user_found:
                user = real_user_found
                # Preserve any new comment from the proxy command
            else:
                # Only check for duplicate proxy if NO real user exists
                for us in self.allNames:
                    if user.name == us.name and user.user_id != us.user_id:
                        return 'AA'
                
                for us in self.allNames:
                    if us.first_name == user.first_name and us.username == user.username and us.user_id != user.user_id:
                        return "AB"
        
        # ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in self.maybeList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return "AB"
            elif us.user_id == user.user_id and us.comment != user.comment:
                us.comment = user.comment
                self._save_user_to_db(user, 'maybe')
                return
        
        # REMOVE FROM OTHER STATES
        for us in self.outList[:]:
            if us.user_id == user.user_id:
                self.outList.remove(us)
                if us in self.allNames:
                    self.allNames.remove(us)
                    
        for us in self.inList[:]:
            if us.user_id == user.user_id:
                self.inList.remove(us)
                if us in self.allNames:
                    self.allNames.remove(us)
        
        if self.inListLimit != None:
            for us in self.waitList[:]:
                if us.user_id == user.user_id:
                    self.waitList.remove(us)
                    if us in self.allNames:
                        self.allNames.remove(us)
                        
            if len(self.inList) < int(self.inListLimit) and len(self.waitList) > 0:
                result = self.waitList[0]
                self.inList.append(self.waitList[0])
                self.waitList.pop(0)
                self.maybeList.append(user)
                if user not in self.allNames:
                    self.allNames.append(user)
                self._save_user_to_db(result, 'in')
                self._save_user_to_db(user, 'maybe')
                logging.info(f"User {user.name} has change his state to maybe")
                return result
        
        self.maybeList.append(user)
        if user not in self.allNames:
            self.allNames.append(user)
        self._save_user_to_db(user, 'maybe')
        logging.info(f"User {user.name} has change his state to maybe")
    
    def _save_user_to_db(self, user, status):
        """Save user to database"""
        if type(user.user_id) == int:
            # Regular user
            db.add_or_update_user(
                self.id,
                user.user_id,
                user.first_name,
                user.username,
                status,
                user.comment
            )
        else:
            # Proxy user
            owner_id = self.proxy_owners.get(user.name)  # or user.user_id
            db.add_or_update_proxy_user(
            self.id,
            user.name,
            status,
            user.comment,
            proxy_owner_id=owner_id
         )
    # --- Proxy owner helpers ---

    def set_proxy_owner(self, proxy_user_id: str, owner_user_id: int):
        """
        Remember who created a proxy user for this rollcall.
        proxy_user_id is the string ID used for proxies (e.g. their name).
        """
        if not hasattr(self, "proxy_owners") or self.proxy_owners is None:
            self.proxy_owners = {}
        self.proxy_owners[proxy_user_id] = owner_user_id

    def get_proxy_owner(self, proxy_user_id: str):
        """
        Get Telegram user_id of the person who created this proxy, if any.
        """
        if not hasattr(self, "proxy_owners") or self.proxy_owners is None:
            return None
        return self.proxy_owners.get(proxy_user_id)


class User:
    """USER OBJECT"""
    def __init__(self, name, username, user_id, allNames):
        self.name = name
        self.first_name = name
        self.username = username
        self.user_id = user_id
        self.comment = ''
        
        # ADD USERNAMES TO NAMES IN NORMAL COMMANDS
        if type(self.user_id) == int:
            for user in allNames:
                if self.name == user.name and self.user_id != user.user_id:
                    self.name = f"{self.name} ({self.username})"
    
    def __str__(self):
        backslash = "\n"
        return f"{self.name + (' (' + self.comment + ')' if self.comment != '' else '')}"
