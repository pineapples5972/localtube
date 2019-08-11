from youtube import util, yt_data_extract, channel
from youtube import yt_app
import settings

import sqlite3
import os
import time
import gevent
import json
import traceback
import contextlib
import defusedxml.ElementTree

import flask
from flask import request


thumbnails_directory = os.path.join(settings.data_dir, "subscription_thumbnails")

# https://stackabuse.com/a-sqlite-tutorial-with-python/

database_path = os.path.join(settings.data_dir, "subscriptions.sqlite")

def open_database():
    if not os.path.exists(settings.data_dir):
        os.makedirs(settings.data_dir)
    connection = sqlite3.connect(database_path, check_same_thread=False)

    # Create tables if they don't exist
    try:
        cursor = connection.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS subscribed_channels (
                              id integer PRIMARY KEY,
                              yt_channel_id text UNIQUE NOT NULL,
                              channel_name text NOT NULL,
                              time_last_checked integer,
                              muted integer DEFAULT 0,
                              upload_frequency integer
                          )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS videos (
                              id integer PRIMARY KEY,
                              sql_channel_id integer NOT NULL REFERENCES subscribed_channels(id) ON UPDATE CASCADE ON DELETE CASCADE,
                              video_id text UNIQUE NOT NULL,
                              title text NOT NULL,
                              duration text,
                              time_published integer NOT NULL,
                              description text
                          )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS tag_associations (
                              id integer PRIMARY KEY,
                              tag text NOT NULL,
                              sql_channel_id integer NOT NULL REFERENCES subscribed_channels(id) ON UPDATE CASCADE ON DELETE CASCADE,
                              UNIQUE(tag, sql_channel_id)
                          )''')

        connection.commit()
    except:
        connection.rollback()
        connection.close()
        raise

    # https://stackoverflow.com/questions/19522505/using-sqlite3-in-python-with-with-keyword
    return contextlib.closing(connection)

def with_open_db(function, *args, **kwargs):
    with open_database() as connection:
        with connection as cursor:
            return function(cursor, *args, **kwargs)

def is_subscribed(channel_id):
    if not os.path.exists(database_path):
        return False

    with open_database() as connection:
        with connection as cursor:
            result = cursor.execute('''SELECT EXISTS(
                                           SELECT 1
                                           FROM subscribed_channels
                                           WHERE yt_channel_id=?
                                           LIMIT 1
                                       )''', [channel_id]).fetchone()
            return bool(result[0])


def _subscribe(cursor, channels):
    ''' channels is a list of (channel_id, channel_name) '''

    # set time_last_checked to 0 on all channels being subscribed to
    channels = ( (channel_id, channel_name, 0) for channel_id, channel_name in channels)

    cursor.executemany('''INSERT OR IGNORE INTO subscribed_channels (yt_channel_id, channel_name, time_last_checked)
                          VALUES (?, ?, ?)''', channels)

# TODO: delete thumbnails
def _unsubscribe(cursor, channel_ids):
    ''' channel_ids is a list of channel_ids '''
    cursor.executemany("DELETE FROM subscribed_channels WHERE yt_channel_id=?", ((channel_id, ) for channel_id in channel_ids))

def _get_videos(cursor, number, offset):
    db_videos = cursor.execute('''SELECT video_id, title, duration, channel_name
                                  FROM videos
                                  INNER JOIN subscribed_channels on videos.sql_channel_id = subscribed_channels.id
                                  ORDER BY time_published DESC
                                  LIMIT ? OFFSET ?''', (number, offset))

    for db_video in db_videos:
        yield {
            'id':   db_video[0],
            'title':    db_video[1],
            'duration': db_video[2],
            'author':   db_video[3],
        }

def _get_subscribed_channels(cursor):
    for item in cursor.execute('''SELECT channel_name, yt_channel_id, muted
                                  FROM subscribed_channels
                                  ORDER BY channel_name COLLATE NOCASE'''):
        yield item


def _add_tags(cursor, channel_ids, tags):
    pairs = [(tag, yt_channel_id) for tag in tags for yt_channel_id in channel_ids]
    cursor.executemany('''INSERT OR IGNORE INTO tag_associations (tag, sql_channel_id)
                          SELECT ?, id FROM subscribed_channels WHERE yt_channel_id = ? ''', pairs)


def _remove_tags(cursor, channel_ids, tags):
    pairs = [(tag, yt_channel_id) for tag in tags for yt_channel_id in channel_ids]
    cursor.executemany('''DELETE FROM tag_associations
                          WHERE tag = ? AND sql_channel_id = (
                              SELECT id FROM subscribed_channels WHERE yt_channel_id = ?
                           )''', pairs)



def _get_tags(cursor, channel_id):
    return [row[0] for row in cursor.execute('''SELECT tag
                                                FROM tag_associations
                                                WHERE sql_channel_id = (
                                                    SELECT id FROM subscribed_channels WHERE yt_channel_id = ?
                                                )''', (channel_id,))]

def _get_all_tags(cursor):
    return [row[0] for row in cursor.execute('''SELECT DISTINCT tag FROM tag_associations''')]

def _get_channel_names(cursor, channel_ids):
    ''' returns list of (channel_id, channel_name) '''
    result = []
    for channel_id in channel_ids:
        row = cursor.execute('''SELECT channel_name
                                FROM subscribed_channels
                                WHERE yt_channel_id = ?''', (channel_id,)).fetchone()
        result.append( (channel_id, row[0]) )
    return result


def _channels_with_tag(cursor, tag, order=False, exclude_muted=False, include_muted_status=False):
    ''' returns list of (channel_id, channel_name) '''

    statement = '''SELECT yt_channel_id, channel_name'''

    if include_muted_status:
        statement += ''', muted'''

    statement += '''
                   FROM subscribed_channels
                   WHERE subscribed_channels.id IN (
                       SELECT tag_associations.sql_channel_id FROM tag_associations WHERE tag=?
                   )
                '''
    if exclude_muted:
        statement += '''AND muted != 1\n'''
    if order:
        statement += '''ORDER BY channel_name COLLATE NOCASE'''

    return cursor.execute(statement, [tag]).fetchall()


units = {
    'year': 31536000,   # 365*24*3600
    'month': 2592000,   # 30*24*3600
    'week': 604800,     # 7*24*3600
    'day':  86400,      # 24*3600
    'hour': 3600,
    'minute': 60,
    'second': 1,
}
def youtube_timestamp_to_posix(dumb_timestamp):
    ''' Given a dumbed down timestamp such as 1 year ago, 3 hours ago,
         approximates the unix time (seconds since 1/1/1970) '''
    dumb_timestamp = dumb_timestamp.lower()
    now = time.time()
    if dumb_timestamp == "just now":
        return now
    split = dumb_timestamp.split(' ')
    number, unit = int(split[0]), split[1]
    if number > 1:
        unit = unit[:-1]    # remove s from end
    return now - number*units[unit]


try:
    existing_thumbnails = set(os.path.splitext(name)[0] for name in os.listdir(thumbnails_directory))
except FileNotFoundError:
    existing_thumbnails = set()


thumbnails_queue = util.RateLimitedQueue()
check_channels_queue = util.RateLimitedQueue()


# Use this to mark a thumbnail acceptable to be retrieved at the request of the browser
# can't simply check if it's in the queue because items are removed when the download starts, not when it finishes
downloading_thumbnails = set()

checking_channels = set()

# Just to use for printing channel checking status to console without opening database
channel_names = dict()

def download_thumbnail_worker():
    while True:
        video_id = thumbnails_queue.get()
        try:
            success = util.download_thumbnail(thumbnails_directory, video_id)
            if success:
                existing_thumbnails.add(video_id)
        except Exception:
            traceback.print_exc()
        finally:
            downloading_thumbnails.remove(video_id)

def check_channel_worker():
    while True:
        channel_id = check_channels_queue.get()
        try:
            _get_upstream_videos(channel_id)
        finally:
            checking_channels.remove(channel_id)

for i in range(0,5):
    gevent.spawn(download_thumbnail_worker)
    gevent.spawn(check_channel_worker)






def download_thumbnails_if_necessary(thumbnails):
    for video_id in thumbnails:
        if video_id not in existing_thumbnails and video_id not in downloading_thumbnails:
            downloading_thumbnails.add(video_id)
            thumbnails_queue.put(video_id)

def check_channels_if_necessary(channel_ids):
    for channel_id in channel_ids:
        if channel_id not in checking_channels:
            checking_channels.add(channel_id)
            check_channels_queue.put(channel_id)



def _get_upstream_videos(channel_id):
    try:
        print("Checking channel: " + channel_names[channel_id])
    except KeyError:
        print("Checking channel " + channel_id)

    videos = []

    channel_videos = channel.extract_info(json.loads(channel.get_channel_tab(channel_id)), 'videos')['items']
    for i, video_item in enumerate(channel_videos):
        if 'description' not in video_item:
            video_item['description'] = ''
        try:
            video_item['time_published'] = youtube_timestamp_to_posix(video_item['published']) - i  # subtract a few seconds off the videos so they will be in the right order
        except KeyError:
            print(video_item)
        videos.append((channel_id, video_item['id'], video_item['title'], video_item['duration'], video_item['time_published'], video_item['description']))

    now = time.time()
    download_thumbnails_if_necessary(video[1] for video in videos if (now - video[4]) < 30*24*3600) # Don't download thumbnails from videos older than a month

    with open_database() as connection:
        with connection as cursor:
            cursor.executemany('''INSERT OR IGNORE INTO videos (sql_channel_id, video_id, title, duration, time_published, description)
                                  VALUES ((SELECT id FROM subscribed_channels WHERE yt_channel_id=?), ?, ?, ?, ?, ?)''', videos)
            cursor.execute('''UPDATE subscribed_channels
                              SET time_last_checked = ?
                              WHERE yt_channel_id=?''', [int(time.time()), channel_id])


def check_all_channels():
    with open_database() as connection:
        with connection as cursor:
            channel_id_name_list = cursor.execute('''SELECT yt_channel_id, channel_name
                                                     FROM subscribed_channels
                                                     WHERE muted != 1''').fetchall()

    channel_names.update(channel_id_name_list)
    check_channels_if_necessary([item[0] for item in channel_id_name_list])


def check_tags(tags):
    channel_id_name_list = []
    with open_database() as connection:
        with connection as cursor:
            for tag in tags:
                channel_id_name_list += _channels_with_tag(cursor, tag, exclude_muted=True)

    channel_names.update(channel_id_name_list)
    check_channels_if_necessary([item[0] for item in channel_id_name_list])


def check_specific_channels(channel_ids):
    with open_database() as connection:
        with connection as cursor:
            channel_id_name_list = []
            for channel_id in channel_ids:
                channel_id_name_list += cursor.execute('''SELECT yt_channel_id, channel_name
                                                          FROM subscribed_channels
                                                          WHERE yt_channel_id=?''', [channel_id]).fetchall()
    channel_names.update(channel_id_name_list)
    check_channels_if_necessary(channel_ids)



@yt_app.route('/import_subscriptions', methods=['POST'])
def import_subscriptions():

    # check if the post request has the file part
    if 'subscriptions_file' not in request.files:
        #flash('No file part')
        return flask.redirect(util.URL_ORIGIN + request.full_path)
    file = request.files['subscriptions_file']
    # if user does not select file, browser also
    # submit an empty part without filename
    if file.filename == '':
        #flash('No selected file')
        return flask.redirect(util.URL_ORIGIN + request.full_path)


    mime_type = file.mimetype

    if mime_type == 'application/json':
        file = file.read().decode('utf-8')
        try:
            file = json.loads(file)
        except json.decoder.JSONDecodeError:
            traceback.print_exc()
            return '400 Bad Request: Invalid json file', 400

        try:
            channels = ( (item['snippet']['resourceId']['channelId'], item['snippet']['title']) for item in file)
        except (KeyError, IndexError):
            traceback.print_exc()
            return '400 Bad Request: Unknown json structure', 400
    elif mime_type in ('application/xml', 'text/xml', 'text/x-opml'):
        file = file.read().decode('utf-8')
        try:
            root = defusedxml.ElementTree.fromstring(file)
            assert root.tag == 'opml'
            channels = []
            for outline_element in root[0][0]:
                if (outline_element.tag != 'outline') or ('xmlUrl' not in outline_element.attrib):
                    continue


                channel_name = outline_element.attrib['text']
                channel_rss_url = outline_element.attrib['xmlUrl']
                channel_id = channel_rss_url[channel_rss_url.find('channel_id=')+11:].strip()
                channels.append( (channel_id, channel_name) )

        except (AssertionError, IndexError, defusedxml.ElementTree.ParseError) as e:
            return '400 Bad Request: Unable to read opml xml file, or the file is not the expected format', 400
    else:
            return '400 Bad Request: Unsupported file format: ' + mime_type + '. Only subscription.json files (from Google Takeouts) and XML OPML files exported from Youtube\'s subscription manager page are supported', 400

    with_open_db(_subscribe, channels)

    return flask.redirect(util.URL_ORIGIN + '/subscription_manager', 303)



@yt_app.route('/subscription_manager', methods=['GET'])
def get_subscription_manager_page():
    group_by_tags = request.args.get('group_by_tags', '0') == '1'
    with open_database() as connection:
        with connection as cursor:
            if group_by_tags:
                tag_groups = []

                for tag in _get_all_tags(cursor):
                    sub_list = []
                    for channel_id, channel_name, muted in _channels_with_tag(cursor, tag, order=True, include_muted_status=True):
                        sub_list.append({
                            'channel_url': util.URL_ORIGIN + '/channel/' + channel_id,
                            'channel_name': channel_name,
                            'channel_id': channel_id,
                            'muted': muted,
                            'tags': [t for t in _get_tags(cursor, channel_id) if t != tag],
                        })

                    tag_groups.append( (tag, sub_list) )

                # Channels with no tags
                channel_list = cursor.execute('''SELECT yt_channel_id, channel_name, muted
                                                 FROM subscribed_channels
                                                 WHERE id NOT IN (
                                                     SELECT sql_channel_id FROM tag_associations
                                                 )
                                                 ORDER BY channel_name COLLATE NOCASE''').fetchall()
                if channel_list:
                    sub_list = []
                    for channel_id, channel_name, muted in channel_list:
                        sub_list.append({
                            'channel_url': util.URL_ORIGIN + '/channel/' + channel_id,
                            'channel_name': channel_name,
                            'channel_id': channel_id,
                            'muted': muted,
                            'tags': [],
                        })

                    tag_groups.append( ('No tags', sub_list) )
            else:
                sub_list = []
                for channel_name, channel_id, muted in _get_subscribed_channels(cursor):
                    sub_list.append({
                        'channel_url': util.URL_ORIGIN + '/channel/' + channel_id,
                        'channel_name': channel_name,
                        'channel_id': channel_id,
                        'muted': muted,
                        'tags': _get_tags(cursor, channel_id),
                    })




    if group_by_tags:
        return flask.render_template('subscription_manager.html',
            group_by_tags = True,
            tag_groups = tag_groups,
        )
    else:
        return flask.render_template('subscription_manager.html',
            group_by_tags = False,
            sub_list = sub_list,
        )

def list_from_comma_separated_tags(string):
    return [tag.strip() for tag in string.split(',') if tag.strip()]


@yt_app.route('/subscription_manager', methods=['POST'])
def post_subscription_manager_page():
    action = request.values['action']

    with open_database() as connection:
        with connection as cursor:
            if action == 'add_tags':
                _add_tags(cursor, request.values.getlist('channel_ids'), [tag.lower() for tag in list_from_comma_separated_tags(request.values['tags'])])
            elif action == 'remove_tags':
                _remove_tags(cursor, request.values.getlist('channel_ids'), [tag.lower() for tag in list_from_comma_separated_tags(request.values['tags'])])
            elif action == 'unsubscribe':
                _unsubscribe(cursor, request.values.getlist('channel_ids'))
            elif action == 'unsubscribe_verify':
                unsubscribe_list = _get_channel_names(cursor, request.values.getlist('channel_ids'))
                return flask.render_template('unsubscribe_verify.html', unsubscribe_list = unsubscribe_list)

            elif action == 'mute':
                cursor.executemany('''UPDATE subscribed_channels
                                      SET muted = 1
                                      WHERE yt_channel_id = ?''', [(ci,) for ci in request.values.getlist('channel_ids')])
            elif action == 'unmute':
                cursor.executemany('''UPDATE subscribed_channels
                                      SET muted = 0
                                      WHERE yt_channel_id = ?''', [(ci,) for ci in request.values.getlist('channel_ids')])
            else:
                flask.abort(400)

    return flask.redirect(util.URL_ORIGIN + request.full_path, 303)

@yt_app.route('/subscriptions', methods=['GET'])
@yt_app.route('/feed/subscriptions', methods=['GET'])
def get_subscriptions_page():
    with open_database() as connection:
        with connection as cursor:
            videos = []
            for video in _get_videos(cursor, 60, 0):
                if video['id'] in downloading_thumbnails:
                    video['thumbnail'] = util.get_thumbnail_url(video['id'])
                else:
                    video['thumbnail'] = util.URL_ORIGIN + '/data/subscription_thumbnails/' + video['id'] + '.jpg'
                video['type'] = 'video'
                video['item_size'] = 'small'
                videos.append(video)

            tags = _get_all_tags(cursor)


            subscription_list = []
            for channel_name, channel_id, muted in _get_subscribed_channels(cursor):
                subscription_list.append({
                    'channel_url': util.URL_ORIGIN + '/channel/' + channel_id,
                    'channel_name': channel_name,
                    'channel_id': channel_id,
                    'muted': muted,
                })

    return flask.render_template('subscriptions.html',
        videos = videos,
        tags = tags,
        subscription_list = subscription_list,
    )

@yt_app.route('/subscriptions', methods=['POST'])
@yt_app.route('/feed/subscriptions', methods=['POST'])
def post_subscriptions_page():
    action = request.values['action']
    if action == 'subscribe':
        if len(request.values.getlist('channel_id')) != len(request.values('channel_name')):
            return '400 Bad Request, length of channel_id != length of channel_name', 400
        with_open_db(_subscribe, zip(request.values.getlist('channel_id'), request.values.getlist('channel_name')))

    elif action == 'unsubscribe':
        with_open_db(_unsubscribe, request.values.getlist('channel_id'))

    elif action == 'refresh':
        type = request.values['type']
        if type == 'all':
            check_all_channels()
        elif type == 'tag':
            check_tags(request.values.getlist('tag_name'))
        elif type == 'channel':
            check_specific_channels(request.values.getlist('channel_id'))
        else:
            flask.abort(400)
    else:
        flask.abort(400)

    return '', 204


@yt_app.route('/data/subscription_thumbnails/<thumbnail>')
def serve_subscription_thumbnail(thumbnail):
    # .. is necessary because flask always uses the application directory at ./youtube, not the working directory
    return flask.send_from_directory(os.path.join('..', thumbnails_directory), thumbnail)

