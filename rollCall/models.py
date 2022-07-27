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
        self.inListLimit=None
        self.waitList= []
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
        return txt+'\n' if len(self.maybeList)>0 else "Maybe:\nNobody\n\n"

    #RETURN WAITLIST
    def waitListText(self):
        backslash="\n"
        txt=f'Waiting:\n'
        i=0
        for user in self.waitList:
            i+=1
            txt+= f"{i}. {user}\n" 
        return (txt+'\n' if len(self.waitList)>0 else "Waiting:\nNobody") if self.inListLimit!=None else ""

    #RETURN ALL THE STATES
    def allList(self):
        backslash="\n"

        txt="Title - "+self.title+"\n"+(self.inListText() if self.inListText()!='In:\nNobody\n\n' else '')+(self.outListText() if self.outListText()!='Out:\nNobody\n\n' else '')+(self.maybeListText() if self.maybeListText()!='Maybe:\nNobody\n\n' else '')+(self.waitListText() if self.waitListText()!='Waiting:\nNobody' else '')
        return txt

    #DELETE A USER
    def delete_user(self, name):
        for us in self.inList:
            if us.name==name:
                self.inList.remove(us)
                for n in allNames:
                    if n.name==name:
                        allNames.remove(n)
                return True

        for us in self.outList:
            if us.name==name:
                self.outList.remove(us)
                for n in allNames:
                    if n.name==name:
                        allNames.remove(n)
                return True

        for us in self.maybeList:
            if us.name==name:
                self.maybeList.remove(us)
                for n in allNames:
                    if n.name==name:
                        allNames.remove(n)
                return True

    #ADD A NEW USER TO IN LIST
    def addIn(self, user):

        #ERROR FOR REPEATLY NAME IN SET COMMANDS
        if type(user.user_id)==str: 
            for us in allNames:
                if user.name==us.name and user.user_id!=us.user_id:
                    return 'AA'

        for us in allNames:
            if us.first_name==user.first_name and us.username == user.username and us.user_id!=user.user_id:
                return "AB"

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        if self.inListLimit==None:
            for us in self.inList:
                if us.user_id == user.user_id and us.comment == user.comment:
                    return "AB"
                elif us.first_name==user.first_name and us.username == user.username and us.user_id!=user.user_id:
                    return "AB"
                elif us.user_id==user.user_id and us.comment != user.comment:
                    us.comment=user.comment
                    return
        else:
            for us in self.inList:
                if us.user_id == user.user_id and us.comment == user.comment:
                    return "AB"
                elif us.first_name==user.first_name and us.username == user.username and us.user_id!=user.user_id:
                    return "AB"
                elif us.user_id==user.user_id and us.comment != user.comment:
                    us.comment=user.comment
                    return

            for us in self.waitList:
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

        #REMOVE THE USER FROM OTHER STATE
        for us in self.waitList:
            if us.user_id == user.user_id:
                self.waitList.remove(us)
                allNames.remove(us)

        #WAITLIST FEATURE. ADD USER TO WAITLIST IF IN LIST IS FULL
        if self.inListLimit!=None:
            if len(self.inList)==int(self.inListLimit):
                self.waitList.append(user)
                allNames.append(user)
                logging.info(f"The user {user.name} has been added to the Wait list")
                return 'AC'

        #ADD THE USER TO THE STATE
        self.inList.append(user)
        allNames.append(user)

        logging.info(f"User {user.name} has change his state to in")

    #ADD A NEW USER TO OUT LIST
    def addOut(self, user):

        #ERROR FOR REPEATLY NAME IN SET COMMANDS
        if type(user.user_id)==str: 
            for us in allNames:
                if user.name==us.name and user.user_id!=us.user_id:
                    return 'AA'

        for us in allNames:
            if us.first_name==user.first_name and us.username == user.username and us.user_id!=user.user_id:
                return "AB"

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in self.outList:
            if us.user_id == user.user_id and us.comment == user.comment:
                return 'AB'
            elif us.first_name==user.first_name and us.username == user.username and us.user_id!=user.user_id:
                return "AB"
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

        if self.inListLimit!=None:
            for us in self.waitList:
                if us.user_id==user.user_id:
                    self.waitList.remove(us)
                    allNames.remove(us)

        if self.inListLimit!=None:
            if len(self.inList)<int(self.inListLimit) and len(self.waitList)>0:
                result=self.waitList[0]
                self.inList.append(self.waitList[0])
                self.waitList.pop(0)
                self.outList.append(user)  
                allNames.append(user)
                logging.info(f"User {user.name} has change his state to out")
                return result

        #ADD THE USER TO THE STATE
        self.outList.append(user)  
        allNames.append(user)

        logging.info(f"User {user.name} has change his state to out")

    #ADD A NEW USER TO MAYBE LIST
    def addMaybe(self, user):

        #ERROR FOR REPEATLY NAME IN SET COMMANDS
        if type(user.user_id)==str: 
            for us in allNames:
                if user.name==us.name and user.user_id!=us.user_id:
                    return 'AA'

        for us in allNames:
            if us.first_name==user.first_name and us.username == user.username and us.user_id!=user.user_id:
                return "AB"

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

        if self.inListLimit!=None:
            for us in self.waitList:
                if us.user_id==user.user_id:
                    self.waitList.remove(us)
                    allNames.remove(us)

        if self.inListLimit!=None:
            if len(self.inList)<int(self.inListLimit) and len(self.waitList)>0:
                result=self.waitList[0]
                self.inList.append(self.waitList[0])
                self.waitList.pop(0)
                self.maybeList.append(user)
                allNames.append(user)
                logging.info(f"User {user.name} has change his state to maybe")
                return result

        #ADD THE USER TO THE STATE
        self.maybeList.append(user)
        allNames.append(user)

        logging.info(f"User {user.name} has change his state to maybe")

    async def setTime(self, time):
        #FUNCTION TO SCHEDULE ROLLCALLS ****NEXT FEATURE***
        pass

class User:

    #USER OBJECT

    def __init__(self, name, username, user_id):
        self.name=name
        self.first_name=name
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

        
    