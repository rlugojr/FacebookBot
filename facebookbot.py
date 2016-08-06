import sys, json, os
from Utils import FacebookAPI as FB, NLP, MongoHelper, simsimi
from Utils.Yelp import yelp_search_v3 as yelp_search
from Speech import processor as STT # Speech to Text
from flask import Flask, request, g, session, render_template, redirect, url_for, jsonify, flash
from flask_oauth import OAuth

from geopy.geocoders import Nominatim # https://github.com/geopy/geopy
from pattern.en import parsetree
from datetime import datetime, timedelta
from pymongo import MongoClient

application = Flask(__name__, instance_relative_config=True)
application.config.from_object('config')
application.config.from_pyfile('config.py', silent=True)

app = application

app.secret_key = 'super secret key'
mongo = MongoClient(app.config['MONGO_URI'])
db = mongo[app.config['MONGO_DBNAME']] # Get database
users = db.users # Get users collection
log = db.message_log # Get log collection
uncategorized_messages = db.uncategorized_messages
memos = db.memos

simSimi = simsimi.SimSimi(
        conversation_language='en',
        conversation_key=app.config['SIMSIMI_KEY']
)

# https://pythonhosted.org/Flask-OAuth/
oauth = OAuth()
facebook = oauth.remote_app('facebook',
    base_url='https://graph.facebook.com/',
    request_token_url=None,
    access_token_url='/oauth/access_token',
    authorize_url='https://www.facebook.com/dialog/oauth',
    consumer_key=app.config['FACEBOOK_APP_ID'],
    consumer_secret=app.config['FACEBOOK_APP_SECRET'],
    request_token_params={'scope': 'email,public_profile'}
)

@app.before_request
def before_request():
    g.user = None
    # g.web_user = None
    # if 'logged_in' in session and session['logged_in']:
    #     data = facebook.get('/me').data
    #     if 'id' in data and 'name' in data:
    #         user_id = data['id']
            # g.web_user = MongoHelper.get_user_mongo(users, user_id)

@app.route('/tos', methods=['GET'])
def tos():
    return render_template('tos.html')

@app.route('/login')
def login():
    print url_for('oauth_authorized')
    return facebook.authorize(callback=url_for('oauth_authorized', _external=True,
        next=request.args.get('next') or request.referrer or None))

@app.route("/logout")
def logout():
    session.pop('logged_in', None)
    session.pop('fb_token', None)
    return redirect(url_for('memo'))

@app.route('/oauth-authorized')
@facebook.authorized_handler
def oauth_authorized(resp):
    next_url = request.args.get('next') or url_for('memo')
    if resp is None:
        flash(u'You denied the request to sign in.')
        return redirect(next_url)

    session['logged_in'] = True
    session['fb_token'] = (
        resp['access_token'],
        ''
    )
    data = facebook.get('/me').data
    if 'name' in data:
        user_name = data['name']

    flash('You are signed in as %s'%(user_name))
    return redirect(next_url)


@facebook.tokengetter
def get_fb_token(token=None):
    return session.get('fb_token')

@app.route('/hi', methods=['GET'])
def hi():
    return render_template('hi.html')

@app.route('/memo/<user_id>', methods=['GET'])
def memo(user_id):
    if 'logged_in' in session and session['logged_in']:
        data = facebook.get('/me').data
        if 'id' in data and 'name' in data:
            # user_id = data['id']
            user_name = data['name']
    else:
        user_name = None
    # if not g.web_user:
    #     return redirect(url_for('hi'))
    # user_name = g.web_user['first_name']
    memo_data = MongoHelper.get_memos_from_user(memos, user_id)['memos']
    return render_template('memo.html', user_name=user_name, memo_data=memo_data)

@app.route('/', methods=['GET'])
def handle_verification():
    print "Handling Verification."
    if request.args.get('hub.verify_token', '') == 'baddest_ass_bot_you_know':
        print "Verification successful!"
        return request.args.get('hub.challenge', '')
    else:
        print "Verification failed!!!"
        return "There's nothing to look here ;)"


@app.route('/', methods=['POST'])
def handle_messages():
    print "Handling Messages"
    payload = request.get_data()
    if app.config['PRINT_INCOMING_PAYLOAD']:
        print payload
    token = app.config['PAT']
    for sender, message in messaging_events(payload):
        if app.config['PRINT_INCOMING_MESSAGE']:
            print "User ID: %s\nMessage:%s" % (sender, message)
        try:
            FB.show_typing(token, sender)
            response = processIncoming(sender, message)
            FB.show_typing(token, sender, 'typing_off')
            if response is not None and response != 'pseudo':
                FB.send_message(token, sender, response)
            elif response != 'pseudo':
                FB.send_message(token, sender, "*scratch my head* :(")
                if NLP.randOneIn(7):
                    FB.send_picture(token, sender, 'https://monosnap.com/file/I6WEAs2xvpZ5qTNmVauNguEzcaRrnI.png')
        except Exception, e:
            print e
            FB.send_message(app.config['PAT'], sender, "Sorry I've got a little bit sick. BRB :(")
            MongoHelper.pop_context(users, g.user)
            if NLP.randOneIn(7):
                FB.send_picture(app.config['PAT'], sender, 'https://monosnap.com/file/3DnnKT60TkUhF93dwjGbNQCaCUK9WH.png')
    return "ok"

temp_audio_url = ""

def processIncoming(user_id, message, just_text=False):
    if not MongoHelper.user_exists(users, user_id): # First time user
        g.user = MongoHelper.get_user_mongo(users, user_id)
        response = "%s %s, nice to meet you :)"%(NLP.sayHiTimeZone(g.user), g.user['first_name'])
        FB.send_picture(app.config['PAT'], user_id, 'https://monosnap.com/file/I6WEAs2xvpZ5qTNmVauNguEzcaRrnI.png')
        # Some functionality introduction here
        return response
    else:
        g.user = MongoHelper.get_user_mongo(users, user_id)

    last_seen = datetime.strptime(g.user['last_seen'],"%Y-%m-%d %H:%M:%S")
    recent5min = datetime.now() - timedelta(minutes=5)

    if last_seen < recent5min:
        MongoHelper.update_last_seen(users, g.user)

    contextData = g.user['contexts']
    
    # Text message type
    if just_text or message['type'] == 'text':
        message_text = message if just_text else message['data']
        incomingMessage = message_text # NLP.removePunctuation(message_text)
        if '.' not in incomingMessage: # help separate sentence for parsetree
            incomingMessage+="."
        s = parsetree(incomingMessage, relations=True, lemmata=True)
        sentence = s[0]
        nounPhrase = NLP.findNounPhrase(sentence)

        if NLP.dismissPreviousRequest(sentence):
            MongoHelper.pop_context(users, g.user)
            return "Sure, no problem"

        if contextData is not None and len(contextData) > 0:
            context = contextData[-1]

            # Find food functionality
            if context['context'] == 'find-food':
                return handle_find_food(user_id, context, sentence, nounPhrase, message, incomingMessage, 'receive_location_text')
            
            elif context['context'] == 'yelp-rename':
                handle_yelp_rename(user_id, g.user, context, message_text)
                MongoHelper.pop_context(users, g.user)
                return "Ta da! %s is now in my cloudy memory :D"%(message_text)

        else:
            if NLP.isGreetings(incomingMessage):
                greeting = "%s %s :D"%(NLP.sayHiTimeZone(g.user), g.user['first_name'])
                FB.send_message(app.config['PAT'], user_id, greeting)
                return "How can I help you?"

            if NLP.isGoodbye(incomingMessage):
                return NLP.sayByeTimeZone(g.user)

            if NLP.isYelp(sentence):                
                return handle_find_food(user_id, None, sentence, nounPhrase, message, incomingMessage, 'receive_request')

            else:
                # Log this message for categorization later
                MongoHelper.log_message(uncategorized_messages, user_id, "text", incomingMessage)
                try:
                    response = simSimi.getConversation(incomingMessage)['response']
                    bad_times = 0
                    while NLP.badWords(response):
                        bad_times += 1
                        print response
                        response = simSimi.getConversation(incomingMessage)['response']
                        if bad_times == 5:
                            return "Hmm... I can't think of anything witty enough to respond to that :P"
                    if 'simsimi' in response:
                        response = response.replace("simsimi", "Optimist Prime")
                    return response
                except simsimi.SimSimiException as e:
                    print e
                    return

    # Location message type
    elif message['type'] == 'location':
        FB.send_message(app.config['PAT'], user_id, "I've received location (%s,%s) (y)"%(message['data'][0],message['data'][1]))

        if contextData is not None and len(contextData) > 0:
            context = contextData[-1]
            if 'context' in context and context['context'] == 'find-food':
                return handle_find_food(user_id, context, None, None, message, None, 'receive_location_gps')
        else:
            return 'pseudo'

    # Audio message type
    elif message['type'] == 'audio':
        audio_url = message['data']

        # Handle Facebook bug when receiving long audio
        # The bug: The app keeps receiving the same POST request
        # This acts as a rescue exit signal
        global temp_audio_url 
        if audio_url == temp_audio_url:
            return 'pseudo'
        temp_audio_url = audio_url

        # Get text from audio
        try:
            message_text = STT.transcribe(audio_url)
            # if 'DISPLAY_STT_RESULT' in os.environ and os.environ['DISPLAY_STT_RESULT'] != 0:
            print message_text
        except Exception, e:
            message_text = "Sorry I can't process that now :("
            FB.send_message(app.config['PAT'], user_id, message_text)
            print e
            return

        # Begin processing audio command
        message_text = message_text.decode('utf-8')

        if NLP.dismissPreviousRequest(message_text, 'string'):
            MongoHelper.pop_context(users, g.user)
            return "Sure, no problem"
        
        if contextData is not None and len(contextData) > 0:
            context = contextData[-1]
            if context == 'create-memo':
                return handle_transcription(user_id, message_text)
    
        if NLP.isMemoCommandOnly(message_text):
            MongoHelper.add_context(users, g.user, 'create-memo')
            return "I'm listening, go ahead :D"

        elif NLP.isMemo(message_text):
            content = NLP.get_memo_content(message_text)
            return handle_transcription(user_id, content)

        return processIncoming(user_id, message_text, True)

    # Quick Reply message type
    elif message['type'] == 'quick_reply':
        context = contextData[-1]
        cmd = message['data']
        
        # cmd: [yelp-more, yelp-ok]
        if cmd == 'yelp-more-yes':
            offset = g.user['yelp_offset'] + 5
            MongoHelper.increment_yelp_offset(users, g.user, 5) # actually update
            result = yelp_search(context['terms'], context['location'], context['coordinates'], 5, offset)

            if result['status'] == 1: # Successful search
                FB.send_message(app.config['PAT'], user_id, "Okay, I've found %s places:"%(len(result['businesses'])))
                FB.send_yelp_results(app.config['PAT'], user_id, result['businesses'])
                FB.send_quick_replies_yelp_search(app.config['PAT'], user_id)
                return 'pseudo'
            else:
                MongoHelper.pop_context(users, g.user)
                MongoHelper.reset_yelp_offset(users, g.user)
                return "That's all I found for now :)"

        elif cmd == 'yelp-more-no':
            context = g.user['contexts'][-1]
            MongoHelper.reset_yelp_offset(users, g.user)

            if context['location_from_memory'] == 1:
                MongoHelper.pop_context(users, g.user)
                return "Glad I can help :)"

            if context['context'] == 'find-food' and context['location'] is not None and context['coordinates'] is not None:
                latest_location = context['location']
                FB.send_quick_replies_yelp_save_location(app.config['PAT'], user_id, latest_location)
                return 'pseudo'

            elif context['context'] == 'find-food' and context['coordinates'] is not None and context['location'] is None:
                return 'pseudo'

            else:
                MongoHelper.pop_context(users, g.user)
                return "Glad I can help :)"
        
        elif cmd == 'yelp-save-location-yes':
            context = g.user['contexts'][-1]
            latest_location = context['location']
            latest_coords = context['coordinates']

            MongoHelper.add_yelp_location_history(users, g.user, latest_coords, latest_location)
            MongoHelper.pop_context(users, g.user)
            MongoHelper.reset_yelp_offset(users, g.user)
            return "Ta da! I wrote it to my cloudy memory :D"

        elif cmd == 'yelp-save-location-no':
            MongoHelper.pop_context(users, g.user)
            MongoHelper.reset_yelp_offset(users, g.user)
            return "OK (y) Hope you like those places I found :D"
        
        elif cmd == 'yelp-save-location-rename':
            context = g.user['contexts'][-1]
            latest_location = context['location']
            latest_coords = context['coordinates']
            contextNow = {'context':'yelp-rename', 
                      'name': latest_location,
                      'coordinates': latest_coords,
                      }
            MongoHelper.pop_context(users, g.user) # pop find-food context
            MongoHelper.add_context(users, g.user, contextNow)
            return "What do you want to call it? :D"

        elif 'yelp-cached-location-' in cmd:
            idx = int(cmd[-1])
            location = get_recent_locations_yelp(idx)
            FB.send_message(app.config['PAT'], user_id, "Looking around %s :D"%(location['name']))
            message = {}
            message['data'] = location['coordinates']
            return handle_find_food(user_id, context, None, None, message, None, 'receive_location_gps', 1)

    else:
        MongoHelper.pop_context(users, g.user)
        MongoHelper.reset_yelp_offset(users, g.user)

def messaging_events(payload):
    """Generate tuples of (sender_id, message_text) from the
    provided payload.
    """
    data = json.loads(payload)
    # print data
    messaging_events = data["entry"][0]["messaging"]
    
    for event in messaging_events:
        sender_id = event["sender"]["id"]
        if "message" in event and "text" in event["message"] and "quick_reply" not in event["message"]:
            data = event["message"]["text"].encode('unicode_escape')
            MongoHelper.log_message(log, sender_id, 'text', data)
            yield sender_id, {'type':'text', 'data': data}

        elif "attachments" in event["message"]:
            if "location" == event['message']['attachments'][0]["type"]:
                coordinates = event['message']['attachments'][
                    0]['payload']['coordinates']
                latitude = coordinates['lat']
                longitude = coordinates['long']

                MongoHelper.log_message(log, sender_id, 'coordinates', str([latitude, longitude]))

                yield sender_id, {'type':'location','data':[latitude, longitude]}

            elif "audio" == event['message']['attachments'][0]["type"]:
                audio_url = event['message'][
                    'attachments'][0]['payload']['url']
                MongoHelper.log_message(log, sender_id, 'audio', audio_url)
                yield sender_id, {'type':'audio','data': audio_url}
            
            else:
                MongoHelper.log_message(log, sender_id, 'other1', event["message"])
                yield sender_id, {'type':'other','data':"I can't echo this"}
        elif "quick_reply" in event["message"]:
            data = event["message"]["quick_reply"]["payload"]
            yield sender_id, {'type':'quick_reply','data': data}
        else:
            MongoHelper.log_message(log, sender_id, 'other2', event["message"])
            yield sender_id, {'type':'other','data':"I can't echo this"}

def get_user_from_message(payload):
    data = json.loads(payload)
    messaging_events = data["entry"][0]["messaging"][-1]
    return messaging_events["sender"]["id"]

def get_recent_locations_yelp(idx=None):
    locations = g.user['yelp_location_history']
    if idx is not None:
        return locations[-idx]
    return locations[-3:] if len(locations) > 3 else locations

def handle_find_food(user_id, context, sentence, nounPhrase, message, incomingMessage, stage, location_from_memory=0):
    if stage == 'receive_request':
        # "Stage 1"
        contextNow = {'context':'find-food', 
                      'location': None,
                      'coordinates': None,
                      'terms': nounPhrase,
                      'location_from_memory': location_from_memory
                      }
        MongoHelper.add_context(users, g.user, contextNow)
        FB.send_message(app.config['PAT'], user_id, "Can you send me your location? :D")
        if len(g.user['yelp_location_history']) > 0:
            FB.send_quick_replies_yelp_suggest_location(app.config['PAT'], user_id, get_recent_locations_yelp())
        return 'pseudo'
        # return "Can you send me your location? :D"
        if NLP.nearBy(sentence):
            # https://fbnewsroomus.files.wordpress.com/2015/06/messenger-location-sharing1-copy.png?w=600&h=568
            MongoHelper.add_context(users, g.user, contextNow)
            return "Can you send me your whereabouts?"

    elif stage == 'receive_location_gps':
        # "Stage 2-GPS"
        if location_from_memory == 1:
            MongoHelper.update_context(users, g.user, 'find-food', 'location_from_memory', 1)

        location = message['data']
        MongoHelper.update_context(users, g.user, 'find-food', 'coordinates', location)
        FB.send_message(app.config['PAT'], user_id, "Looking looking... :D")

        result = yelp_search(context['terms'], None, location)
        if result['status'] == 1:
            FB.send_message(app.config['PAT'], user_id, "Okay, I've found %s places:"%(len(result['businesses'])))
            FB.send_yelp_results(app.config['PAT'], user_id, result['businesses'])
            FB.send_quick_replies_yelp_search(app.config['PAT'], user_id)
            return 'pseudo'
        else:    
            return "Sorry I couldn't find anything :("

    elif stage == 'receive_location_text':
        # "Stage 2 - Text"

        if context['location'] == None and context['coordinates'] == None:
            context['location'] = nounPhrase
            try:
                geolocator = Nominatim()
                location_lookup = geolocator.geocode(nounPhrase)
                coords = [location_lookup.latitude, location_lookup.longitude]
                MongoHelper.update_context(users, g.user, 'find-food', 'coordinates', coords)
                MongoHelper.update_context(users, g.user, 'find-food', 'location', nounPhrase)
                FB.send_message(app.config['PAT'], user_id, "Looking looking... :D")
                result = yelp_search(context['terms'], None, coords)

            except Exception, e:
                print e
                MongoHelper.update_context(users, g.user, 'find-food', 'location', nounPhrase)
                FB.send_message(app.config['PAT'], user_id, NLP.oneOf(["Sure, give me a few seconds... B-)", "Scanning the world... :D", "Zoom zoom zoom...", "Going into the Food Cerebro... B-)", "Believe me, I'm a foodie, not an engineer..."]))
                result = yelp_search(context['terms'], nounPhrase)
            
            if result['status'] == 1: # Successful search
                FB.send_message(app.config['PAT'], user_id, "Okay, I've found %s places:"%(len(result['businesses'])))
                FB.send_yelp_results(app.config['PAT'], user_id, result['businesses'])
                FB.send_quick_replies_yelp_search(app.config['PAT'], user_id)
                return 'pseudo'
            else:
                MongoHelper.pop_context(users, g.user)
                return "Sorry I can't find any places for that :("
                # Follow up
        else:
            MongoHelper.pop_context(users, g.user)
            return

def handle_yelp_rename(user_id, user, context, name):
    MongoHelper.add_yelp_location_history(users, user, context['coordinates'], name)

def handle_transcription(user_id, message_text):
    user = MongoHelper.get_memo_user(memos, user_id)
    if len(message_text.split(" ")) > 10:
        MongoHelper.add_memo(memos, user, message_text)
        url = url_for("memo", user_id=user_id, _external=True)
        FB.send_url(app.config['PAT'], user_id, "I've saved it for you :D", url)
    MongoHelper.pop_context(users, g.user)
    return 'pseudo'

if __name__ == '__main__':
    if len(sys.argv) == 2:
        app.run(port=int(sys.argv[1]))
    else:
        app.run()