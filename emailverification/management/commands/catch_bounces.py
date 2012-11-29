from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from django.conf import settings

from optparse import make_option

import imaplib, email, re
from datetime import datetime, timedelta

from emailverification.models import BouncedEmail

class Command(BaseCommand):
	args = ''
	help = 'Read an IMAP account for bounced mail to find user accounts we should stop emailing and records bounces in BouncedEmail model instances.'
	
	def handle(self, *args, **options):
		# Connect to IMAP server and log in.
		clz = imaplib.IMAP4 if not settings.BOUNCES_IMAP_SSL else imaplib.IMAP4_SSL
		server = clz(settings.BOUNCES_IMAP_HOST)
		
		ret, msg = server.login(settings.BOUNCES_IMAP_USER, settings.BOUNCES_IMAP_PASSWORD)
		assert ret == "OK"
		
		ret, msg = server.select()
		assert ret == "OK"
		
		# Scan all mail in the mailbox.
		bounces_by_status = { }
		typ, data = server.search(None, 'ALL')
		for num in data[0].split():
			# Load the message and parse it.
			typ, data = server.fetch(num, '(RFC822)')
			msg = email.message_from_string(data[0][1])
			
			# Only read multipart/report messages since we can parse errors out of them.
			if msg.get_content_type() != 'multipart/report': continue
			
			# Check that it is a bounce to an address that matches the EMAIL_UPDATES_RETURN_PATH
			# setting, and get out of that the original delivery user ID.
			m = settings.BOUNCES_UID_REGEX.match(msg['X-Original-To'])
			if not m: continue
			uid = int(m.group(1))
			
			# Look for the parsable report section and check if this is a permanent failure
			# that warrants disabling email updates for the user.
			for part in msg.walk():
				if part.get_content_type() != 'message/delivery-status': continue
				m = re.search(r"Diagnostic-Code: smtp; \d+ ([\d\.]+)", str(part))
				if not m: m = re.search(r"Status: (.*)", str(part)) # fall back to more generic code
				if not m: continue
				status = m.group(1)
				bounces_by_status[status] = bounces_by_status.get(status, 0) + 1
				
				# If the status is one of these, don't record the bounce.
				# 5.1.1: Invalid mailbox.
				# 5.1.6: Invalid mailbox.
				# 5.2.1: Mailbox disabled.
				# 5.4.4: DNS lookup failure (usually reported by our own MTA).
				# 5.7.1: Relaying denied, but often says user unknown.
				# 5.0.0: Generic permanent error often used for invalid mailbox.
				if status not in ("5.0.0", "5.1.1", "5.1.6", "5.2.1", "5.4.4", "5.7.1"): continue
				
				# record the bounce
				u = User.objects.get(id=uid)
				be, is_new = BouncedEmail.objects.get_or_create(user=u)
				if not is_new:
					be.bounces += 1
					be.save()
				
				# delete the message
				server.store(num, '+FLAGS', r'\Deleted')
				
				# only need to hit one message part per message
				break
				
		server.expunge()
		server.close()
		server.logout()
		
		print "Bounces by status code:"
		for k, v in sorted(bounces_by_status.items(), key = lambda kv : kv[1], reverse=True):
			print k, "\t", v
