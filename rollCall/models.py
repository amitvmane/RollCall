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
def _parse_db_datetime(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%d-%m-%Y %H:%M",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        raise ValueError(f"Unsupported datetime string format: {value}")

    raise TypeError(f"Unsupported datetime value type: {type(value)}")


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
                logging.info(f"[RC #{self.id}] Created rollcall '{title}' for chat {chat_id}")
                self.chat_id = chat_id
                db.ensure_rollcall_stats(self.id)
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
        raw_limit = data['in_list_limit']
        try:
            self.inListLimit = int(raw_limit) if raw_limit is not None else None
        except (ValueError, TypeError):
            logging.warning(f"[RC] Invalid in_list_limit from DB: {raw_limit!r}, defaulting to None")
            self.inListLimit = None
        self.reminder = data['reminder_hours']
        self.proxy_owners = {}


        # Parse datetime from database safely for both SQLite and PostgreSQL
        self.finalizeDate = _parse_db_datetime(data.get('finalize_date'))
        self.createdDate = _parse_db_datetime(data.get('created_at')) or datetime.utcnow()
        # Load users from database
        self._load_users_from_db()

    def _load_users_from_db(self):
        """Load all users from database preserving true join order across real and proxy users."""
        self.inList   = []
        self.outList  = []
        self.maybeList = []
        self.waitList  = []
        self.allNames  = []
        self.proxy_owners = {}

        # Pre-fetch all proxy names to detect display name conflicts
        all_proxy_names = set()
        for status in ['in', 'out', 'maybe', 'waitlist']:
            for proxy_data in db.get_proxy_users_by_status(self.id, status):
                all_proxy_names.add(proxy_data['name'])

        STATUS_ORDER = {'in': 1, 'out': 2, 'maybe': 3, 'waitlist': 4}
        combined = []  # list of (status_order, pos, created_at, user_object)

        # --- Real users ---
        for user_data in db.get_all_users(self.id):
            user = User.__new__(User)
            user.user_id    = user_data['user_id']
            user.first_name = user_data['first_name']
            user.username   = user_data['username']
            user.comment    = user_data['comment'] or ''
            if user_data['first_name'] in all_proxy_names and user_data['username']:
                user.name = f"{user_data['first_name']} (@{user_data['username']})"
            else:
                user.name = user_data['first_name']

            status = user_data['status']
            if status == 'in':
                pos = user_data.get('in_pos') or 0
            elif status == 'out':
                pos = user_data.get('out_pos') or 0
            elif status == 'waitlist':
                pos = user_data.get('wait_pos') or 0
            else:
                pos = 0

            combined.append((STATUS_ORDER.get(status, 5), pos, user_data.get('created_at', ''), user, status))

        # --- Proxy users ---
        for status in ['in', 'out', 'maybe', 'waitlist']:
            for proxy_data in db.get_proxy_users_by_status(self.id, status):
                user = User.__new__(User)
                user.user_id    = proxy_data['name']
                user.first_name = proxy_data['name']
                user.name       = proxy_data['name']
                user.username   = None
                user.comment    = proxy_data['comment'] or ''

                if status == 'in':
                    pos = proxy_data.get('in_pos') or 0
                elif status == 'out':
                    pos = proxy_data.get('out_pos') or 0
                elif status == 'waitlist':
                    pos = proxy_data.get('wait_pos') or 0
                else:
                    pos = 0

                combined.append((STATUS_ORDER.get(status, 5), pos, proxy_data.get('created_at', ''), user, status))

                owner_id = proxy_data.get('proxy_owner_id')
                if owner_id is not None:
                    self.proxy_owners[user.user_id] = owner_id

        # Sort by: status bucket → position → created_at (fallback)
        combined.sort(key=lambda x: (x[0], x[1], x[2] or ''))

        # Distribute into lists
        for _, _, _, user, status in combined:
            if status == 'in':
                self.inList.append(user)
            elif status == 'out':
                self.outList.append(user)
            elif status == 'maybe':
                self.maybeList.append(user)
            elif status == 'waitlist':
                self.waitList.append(user)
            self.allNames.append(user)

    def _get_user_current_status(self, user):
        """Return current status string for a user object."""
        if user in self.inList:
            return 'in'
        elif user in self.outList:
            return 'out'
        elif user in self.maybeList:
            return 'maybe'
        elif user in self.waitList:
            return 'waitlist'
        logging.warning(f"[RC #{self.id}] _get_user_current_status: user {repr(user)} not found in any list")
        return None

    def _resolve_display_name_conflict(self, user):
        """
        Ensures real user and proxy with same first_name can coexist.
        - If real user being added and proxy with same name exists:
          → set real user display name to 'FirstName (@username)'
        - If proxy being added and real user with same name exists:
          → update that real user's display name to 'FirstName (@username)'
          → persist updated name to DB
        """
        if type(user.user_id) == int:
            # Real user — check if any proxy with same first_name exists
            proxy_exists = any(
                us.first_name == user.first_name and type(us.user_id) == str
                for us in self.allNames
            )
            if proxy_exists:
                if user.username:
                    user.name = f"{user.first_name} (@{user.username})"
                else:
                    user.name = f"{user.first_name} (real)"
        else:
            # Proxy being added — check if real user with same first_name already exists
            # Update that real user's display name to distinguish
            for us in self.allNames:
                if us.first_name == user.first_name and type(us.user_id) == int:
                    if us.username:
                        us.name = f"{us.first_name} (@{us.username})"
                    else:
                        us.name = f"{us.first_name} (real)"
                    # Persist updated display name to DB
                    status = self._get_user_current_status(us)
                    if status is not None:
                        self._save_user_to_db(us, status)
                    break


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
                logging.info(f"[RC #{self.id} '{self.title}'] User '{name}' deleted")
                # Reload from database to sync
                self._load_users_from_db()

                # NEW: clean proxy_owners mapping (proxies use name as user_id)
                try:
                    if hasattr(self, "proxy_owners") and self.proxy_owners is not None:
                        if name in self.proxy_owners:
                            del self.proxy_owners[name]
                except Exception:
                    logging.error(traceback.format_exc())

                return True

            return False

        except:
            logging.error(traceback.format_exc())
            return False
        
    def addIn(self, user):
        logging.debug(f"allNames: {[repr(u) for u in self.allNames]}")
        if type(user.user_id) == str:
            # PROXY USER — only block if a DIFFERENT proxy with same name exists
            for us in self.allNames:
                if user.name == us.name and type(us.user_id) == str:
                    break  # already tracked — this is a status change, allow through
            else:
                # New proxy not yet in allNames — resolve display name conflict
                self._resolve_display_name_conflict(user)
        else:
            # REAL USER — block only real-vs-real duplicate identity
            for us in self.allNames:
                if (
                    us.first_name == user.first_name and
                    us.username == user.username and
                    us.user_id != user.user_id and
                    type(us.user_id) == int
                ):
                    return "AB"
            # If proxy with same first_name exists → update this real user's display name
            self._resolve_display_name_conflict(user)

        if self.inListLimit is None:
            for us in self.inList:
                if us.user_id == user.user_id and us.comment == user.comment:
                    return "AB"
                elif us.user_id == user.user_id and us.comment != user.comment:
                    us.comment = user.comment
                    self._save_user_to_db(user, 'in')
                    return
        else:
            for us in self.inList:
                if us.user_id == user.user_id and us.comment == user.comment:
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

        if self.inListLimit is not None:
            if len(self.inList) >= int(self.inListLimit):
                self.waitList.append(user)
                if user not in self.allNames:
                    self.allNames.append(user)
                self._save_user_to_db(user, 'waitlist')
                logging.info(f"[RC #{self.id} '{self.title}'] {repr(user)} → WAITING (limit={self.inListLimit})")
                return 'AC'

        self.inList.append(user)
        if user not in self.allNames:
            self.allNames.append(user)
        self._save_user_to_db(user, 'in')
        logging.info(f"[RC #{self.id} '{self.title}'] {repr(user)} → IN")



    # ADD A NEW USER TO OUT LIST
    def addOut(self, user):
        logging.debug(f"allNames: {[repr(u) for u in self.allNames]}")
        if type(user.user_id) == str:
            # PROXY USER — only block if a DIFFERENT proxy with same name exists
            for us in self.allNames:
                if user.name == us.name and type(us.user_id) == str:
                    break  # already tracked — this is a status change, allow through
            else:
                # New proxy not yet in allNames — resolve display name conflict
                self._resolve_display_name_conflict(user)
        else:
            # REAL USER — block only real-vs-real duplicate identity
            for us in self.allNames:
                if (
                    us.first_name == user.first_name and
                    us.username == user.username and
                    us.user_id != user.user_id and
                    type(us.user_id) == int
                ):
                    return "AB"
            self._resolve_display_name_conflict(user)

        for us in self.outList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return 'AB'
            elif us.user_id == user.user_id and us.comment != user.comment:
                us.comment = user.comment
                self._save_user_to_db(user, 'out')
                return

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

        if self.inListLimit is not None:
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
                logging.info(f"[RC #{self.id} '{self.title}'] {repr(user)} → OUT")
                return result

        self.outList.append(user)
        if user not in self.allNames:
            self.allNames.append(user)
        self._save_user_to_db(user, 'out')
        logging.info(f"[RC #{self.id} '{self.title}'] {repr(user)} → OUT")

    # ADD A NEW USER TO MAYBE LIST
    def addMaybe(self, user):
        logging.debug(f"allNames: {[repr(u) for u in self.allNames]}")
        if type(user.user_id) == str:
            # PROXY USER — only block if a DIFFERENT proxy with same name exists
            for us in self.allNames:
                if user.name == us.name and type(us.user_id) == str:
                    break  # already tracked — this is a status change, allow through
            else:
                # New proxy not yet in allNames — resolve display name conflict
                self._resolve_display_name_conflict(user)
        else:
            # REAL USER — block only real-vs-real duplicate identity
            for us in self.allNames:
                if (
                    us.first_name == user.first_name and
                    us.username == user.username and
                    us.user_id != user.user_id and
                    type(us.user_id) == int
                ):
                    return "AB"
            self._resolve_display_name_conflict(user)

        for us in self.maybeList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return "AB"
            elif us.user_id == user.user_id and us.comment != user.comment:
                us.comment = user.comment
                self._save_user_to_db(user, 'maybe')
                return

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

        if self.inListLimit is not None:
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
                logging.info(f"[RC #{self.id} '{self.title}'] {repr(user)} → MAYBE")
                return result

        self.maybeList.append(user)
        if user not in self.allNames:
            self.allNames.append(user)
        self._save_user_to_db(user, 'maybe')
        logging.info(f"[RC #{self.id} '{self.title}'] {repr(user)} → MAYBE")



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

    @property
    def db_id(self):
        """Alias for self.id — provides compatibility with code using rc.db_id"""
        return self.id

    @db_id.setter
    def db_id(self, value):
        self.id = value

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
                    suffix = f"@{self.username}" if self.username else str(self.user_id)
                    self.name = f"{self.name} ({suffix})"
    
    def __str__(self):
        backslash = "\n"
        return f"{self.name + (' (' + self.comment + ')' if self.comment != '' else '')}"
    
    def __repr__(self):
        return f"User(name={self.name!r}, @{self.username}, id={self.user_id})"
    