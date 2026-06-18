-- send_imessage.applescript
-- Usage: osascript send_imessage.applescript "+15551234567" "Your message here"
-- Sends an iMessage through the Messages app. Requires macOS, Messages signed in,
-- and Automation permission granted to your terminal / Python.

on run {targetPhone, targetMessage}
	tell application "Messages"
		set targetService to 1st account whose service type = iMessage
		set targetBuddy to participant targetPhone of targetService
		send targetMessage to targetBuddy
	end tell
end run
