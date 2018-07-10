import mimetypes
import urllib.parse
from youtube import local_playlist, watch, search, playlist, channel, comments, common
YOUTUBE_FILES = (
    "/shared.css",
    "/opensearch.xml",
    '/comments.css',
)

def youtube(env, start_response):
    path, method, query_string = env['PATH_INFO'], env['REQUEST_METHOD'], env['QUERY_STRING']
    if method == "GET":
        if path in YOUTUBE_FILES:
            with open("youtube" + path, 'rb') as f:
                mime_type = mimetypes.guess_type(path)[0] or 'application/octet-stream'
                start_response('200 OK',  (('Content-type',mime_type),) )
                return f.read()

        elif path == "/comments":
            start_response('200 OK',  (('Content-type','text/html'),) )
            return comments.get_comments_page(query_string).encode()

        elif path == "/watch":
            start_response('200 OK',  (('Content-type','text/html'),) )
            return watch.get_watch_page(query_string).encode()
        
        elif path == "/search":
            start_response('200 OK',  (('Content-type','text/html'),) )
            return search.get_search_page(query_string).encode()
        
        elif path == "/playlist":
            start_response('200 OK',  (('Content-type','text/html'),) )
            return playlist.get_playlist_page(query_string).encode()
        
        elif path.startswith("/channel/"):
            start_response('200 OK',  (('Content-type','text/html'),) )
            return channel.get_channel_page(path[9:], query_string=query_string).encode()

        elif path.startswith("/user/"):
            start_response('200 OK',  (('Content-type','text/html'),) )
            return channel.get_user_page(path[6:], query_string=query_string).encode()

        elif path.startswith("/playlists"):
            start_response('200 OK',  (('Content-type','text/html'),) )
            return local_playlist.get_playlist_page(path[10:], query_string=query_string).encode()
        elif path.startswith("/api/"):
            start_response('200 OK',  () )
            return common.fetch_url('https://www.youtube.com' + path + ('?' + query_string if query_string else ''))
        else:
            start_response('404 Not Found',  () )
            return b'404 Not Found'

    elif method == "POST":
        if path == "/edit_playlist":
            fields = urllib.parse.parse_qs(env['wsgi.input'].read().decode())
            if fields['action'][0] == 'add':
                local_playlist.add_to_playlist(fields['playlist_name'][0], fields['video_info_list'])
                
            start_response('204 No Content', ())
        else:
            start_response('404 Not Found', ())
            return b'404 Not Found' 

    else:
        start_response('501 Not Implemented', ())
        return b'501 Not Implemented'