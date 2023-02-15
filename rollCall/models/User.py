#CLASS TO MANAGE USER OBJECTS      
class User:

    #USER OBJECT
    def __init__(self, first_name, username, user_id):
        self.name = first_name
        self.first_name = first_name
        self.username = username
        self.user_id = user_id
        self.comment = ''
        self.last_state = None

    def __str__(self):
        backslash="\n"
        return f"{self.name + (' ('+self.comment+')' if self.comment!='' else '')}"