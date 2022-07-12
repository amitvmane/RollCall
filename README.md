Telegram bot that helps you keep track of users who is attending an event within a group chat.

Basic Commands

	/start_roll_call - Start a new roll call (with optional title) alias /src
	/end_roll_call - End the current roll call alias /erc
	/in - Let everyone know you'll be attending (with optional comment)
	/out - Let everyone know you won't be attending (with optional comment)
	/maybe - Let everyone know that you don't know (with optional comment)
	/whos_in - List attendees
	/whos_out - List attendees

Other Commands

	/set_title {title} - Add a title to the current roll call alias /st
	/set_in_for {name} - Allows you to respond for another user alias /sif
	/set_out_for {name} - Allows you to respond for another user alias /sof
	/set_maybe_for {name} - Allows you to respond for another user alias /smf
	/shh - Tells WhosInBot not to list all attendees after every response
	/louder - Tells WhosInBot to list all attendees after every response

Usage

	User needs to include given bot as admin in telegram group. 
	In order to initiate a roll call , User can use command /start_roll_call ( with optional title ).
	Group members can use commands like in,out,maybe etc to convey the availability with optional justification comment/reason.
	
	
Technical Details 
	
		Source code is available in python script and it can be enahnced/streanlined based on future usage. 
		
Deployment Details
		
		Currently bot needs to be registered under botfather and can be run as python script. 
		Container based deployment will carried out in subsequent phases. 
		
Upcoming Enahncements 
		
		Refer to Issues/Enhancement section. 
		
Issue/Bug report
	
		Please file issues with detail steps and screenshots for troubleshooting. 
		
		
	
