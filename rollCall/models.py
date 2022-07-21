import logging
import datetime

from config import TELEGRAM_TOKEN
from exceptions import *

#LIST WITH ALL USER NAMES. IT IS USED TO DETECT IF THERE ARE REPEATED NAMES
allNames=[]



class RollCall:
    #THIS IS THE ROLLCALL OBJECT

    def __init__(self, title, finishDate):
        self.title= title
        self.inList= []
        self.outList= []
        self.maybeList= []
        self.createdDate= datetime.datetime.utcnow

    #RETURN INLIST
    def inListText(self):
        backslash="\n"
        txt=f'In:\n'
        i=0
        for user in self.inList:
            i+=1
            txt+= f"{i}. {user}\n"
        return txt+'\n' if len(self.inList)>0 else "In:\nNobody\n\n"
    
    #RETURN OUTLIST
    def outListText(self):
        backslash="\n"
        txt=f'Out:\n'
        i=0
        for user in self.outList:
            i+=1
            txt+= f"{i}. {user}\n" 
        return txt+'\n' if len(self.outList)>0 else "Out:\nNobody\n\n"
    
    #RETURN MAYBELIST
    def maybeListText(self):
        backslash="\n"
        txt=f'Maybe:\n'
        i=0
        for user in self.maybeList:
            i+=1
            txt+= f"{i}. {user}\n" 
        return txt+'\n' if len(self.maybeList)>0 else "Maybe:\nNobody"

    def allList(self):
        backslash="\n"

        txt="Title - "+self.title+"\n"+(self.inListText() if self.inListText()!='In:\nNobody\n\n' else '')+(self.outListText() if self.outListText()!='Out:\nNobody\n\n' else '')+(self.maybeListText() if self.maybeListText()!='Maybe:\nNobody' else '')
        return txt

    #DELETE A USER
    def delete_user(self, name):
        for us in self.inList:
            if us.name==name:
                self.inList.remove(us)
                return True

        for us in self.outList:
            if us.name==name:
                self.outList.remove(us)
                return True

        for us in self.maybeList:
            if us.name==name:
                self.maybeList.remove(us)
                return True

    #ADD A NEW USER TO IN LIST
    def addIn(self, user):

        if type(user.user_id)==str: 
            for us in allNames:
                if user.name==us.name and user.user_id!=us.user_id:
                    return 'AA'

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in self.inList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return "AB"
            elif us.user_id==user.user_id and us.comment != user.comment:
                us.comment=user.comment
                return

        #REMOVE THE USER FROM OTHER STATE
        for us in self.outList:
            if us.user_id == user.user_id:
                self.outList.remove(us)
                allNames.remove(us)

        #REMOVE THE USER FROM OTHER STATE
        for us in self.maybeList:
            if us.user_id == user.user_id:
                self.maybeList.remove(us)
                allNames.remove(us)

        #ADD THE USER TO THE STATE
        self.inList.append(user)
        allNames.append(user)

        print(len(self.inList) + len(self.outList) + len(self.maybeList))

        logging.info(f"User {user.name} has change his state to in")

    #ADD A NEW USER TO OUT LIST
    def addOut(self, user):

        if type(user.user_id)==str: 
            for us in allNames:
                if user.name==us.name and user.user_id!=us.user_id:
                    return 'AA'

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in self.outList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return 'AB'
            elif us.user_id==user.user_id and us.comment != user.comment:
                us.comment=user.comment
                return

        #REMOVE THE USER FROM OTHER STATE
        for us in self.inList:
            if us.user_id == user.user_id:
                self.inList.remove(us)
                allNames.remove(us)

        #REMOVE THE USER FROM OTHER STATE
        for us in self.maybeList:
            if us.user_id == user.user_id:
                self.maybeList.remove(us)
                allNames.remove(us)

        #ADD THE USER TO THE STATE
        self.outList.append(user)  
        allNames.append(user)

        print(len(self.inList) + len(self.outList) + len(self.maybeList))

        logging.info(f"User {user.name} has change his state to out")

    #ADD A NEW USER TO MAYBE LIST
    def addMaybe(self, user):

        if type(user.user_id)==str: 
            for us in allNames:
                if user.name==us.name and user.user_id!=us.user_id:
                    return 'AA'

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in self.maybeList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return "AB" 
            elif us.user_id==user.user_id and us.comment != user.comment:
                us.comment=user.comment
                return

        #REMOVE THE USER FROM OTHER STATE
        for us in self.outList:
            if us.user_id == user.user_id:
                self.outList.remove(us)
                allNames.remove(us)

        #REMOVE THE USER FROM OTHER STATE
        for us in self.inList:
            if us.user_id == user.user_id:
                self.inList.remove(us)
                allNames.remove(us)

        #ADD THE USER TO THE STATE
        self.maybeList.append(user)
        allNames.append(user)
    
        print(len(self.inList) + len(self.outList) + len(self.maybeList))

        logging.info(f"User {user.name} has change his state to maybe")

    async def setTime(self, time):
        #FUNCTION TO SCHEDULE ROLLCALLS ****NEXT FEATURE***
        pass

class User:

    #USER OBJECT

    def __init__(self, name, username, user_id):
        self.name=name
        self.username=username
        self.user_id=user_id
        self.comment=''

        #ADD USERNAMES TO NAMES IN NORMAL COMMANDS (/IN, /OUT, /MAYBE)
        if type(self.user_id)==int:
            for user in allNames:
                if self.name == user.name and self.user_id != user.user_id:
                    self.name=f"{self.name} ({self.username})"

    def __str__(self):
        backslash="\n"
        return f"{self.name + (' ('+self.comment+')' if self.comment!='' else '')}"

        
    