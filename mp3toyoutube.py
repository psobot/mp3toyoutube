import os
import yaml
import math
import flask
import flask.logging
import logging
import argparse
import tempfile
import requests
import subprocess
import webbrowser
import gdata.youtube
import gdata.youtube.service

from mutagen.mp3 import MP3
from threading import Thread, Lock

DEFAULT_API_KEY_PATH = "apikeys.yml"

log = logging.getLogger(__name__)
log.addHandler(logging.StreamHandler())
log.setLevel(logging.INFO)

logging.getLogger('requests').setLevel(logging.DEBUG)


class MP3ToYoutube(object):
    def __init__(self, api_key_path, token, description,
                 category, keywords, image, private, **kwargs):
        self.yt = gdata.youtube.service.YouTubeService()
        try:
            self.keys = yaml.load(open(api_key_path, 'r'))
        except IOError:
            log.critical("Could not open API key file!")
            if api_key_path == DEFAULT_API_KEY_PATH:
                log.info("Please create a file at '%s' from the given "
                         ".sample file.", DEFAULT_API_KEY_PATH)
            raise

        self.yt.developer_key = self.keys['developer_key']
        self.yt.SetAuthSubToken(token or self.get_access_token(12345))

        if description == "-":
            self.description = self.get_lines("Please enter a video description, "
                                                "followed by a blank line.")
        else:
            self.description = description

        self.category = category
        self.keywords = keywords
        self.private = private
        self.image = image
        self.code = None

    def upload(self, video_file_location, title, description, category, keywords):
        log.info("Uploading new video with title %s and category %s...", title, category)
        my_media_group = gdata.media.Group(
            title=gdata.media.Title(text=title),
            description=gdata.media.Description(description_type='plain',
                                                text=description),
            keywords=gdata.media.Keywords(text=", ".join(keywords)),
            category=[gdata.media.Category(
                text=category,
                label=category,
                scheme='http://gdata.youtube.com/schemas/2007/categories.cat'
            )],
            player=None,
            private=gdata.media.Private() if self.private else None,
        )
        video_entry = gdata.youtube.YouTubeVideoEntry(media=my_media_group)
        inserted = self.yt.InsertVideoEntry(video_entry, video_file_location)
        log.info("Video %s uploaded!", title)
        return inserted

    def transcode(self, artwork_path, mp3_path, tag):
        assert artwork_path is not None
        f = tempfile.NamedTemporaryFile(suffix='.avi')
        cmd = ['ffmpeg',
            '-loop', '1',
            '-shortest',
            '-r', '1/%d' % math.ceil(tag.info.length + 1),
            '-y',
            '-i', artwork_path,
            '-i', mp3_path,
            '-acodec', 'copy',
            '-vcodec', 'copy',
            f.name]
        log.info("Rendering %s to video...", os.path.basename(mp3_path))
        subprocess.Popen(cmd, stderr=open('/dev/null', 'w')).wait()
        log.info("Render of %s complete!", os.path.basename(mp3_path))
        return f

    def get_access_token(self, port):
        url = "https://accounts.google.com/o/oauth2/auth"
        params = {
            "client_id": self.keys['client_id'],
            'redirect_uri': "http://localhost:%d" % port,
            'scope': 'https://gdata.youtube.com',
            'response_type': 'code',
            'access_type': 'offline',
        }
        webbrowser.open("%s?%s" % (url, "&".join(["=".join([k, v])
                                                for k, v in params.iteritems()])))

        #   OH GOD WHAT HAVE I DONE
        app = flask.Flask(__name__)
        lock = Lock()

        @app.route("/")
        def receive_callback():
            self.code = flask.request.args.get('code')
            lock.release()
            return "Success! You may now close this browser window."

        def run():
            lock.acquire()
            app.run(port=port)
        t = Thread(target=run)
        t.setDaemon(True)
        t.start()

        log.info("Waiting for response from Google via your browser...")
        lock.acquire()
        resp = requests.post('https://accounts.google.com/o/oauth2/token',
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": 'authorization_code',
                "code": str(self.code),
                "client_id": self.keys['client_id'],
                "client_secret": self.keys['client_secret'],
                "redirect_uri": "http://localhost:%d" % port,
            }
        )
        resp.raise_for_status()
        token = resp.json()['access_token']
        log.info("Got access token: %s", token)
        return token

    def get_lines(self, prompt):
        print prompt

        def read():
            while True:
                line = raw_input()
                if not line:
                    break
                yield line
        return "".join(list(read))

    def process(self, files, image=None):
        for audio_file in files:
            tag = MP3(audio_file)
            if 'APIC:' in tag:
                ext = ''
                if tag['APIC:'].mime == "image/jpeg":
                    ext = '.jpg'
                elif tag['APIC:'].mime == "image/png":
                    ext = '.png'
                else:
                    raise NotImplementedError("Only PNG or JPEG artwork "
                                              "is currently supported.")
                with tempfile.NamedTemporaryFile(suffix=ext) as art_file:
                    art_file.write(tag['APIC:'].data)
                    art_file.flush()
                    f = self.transcode(art_file.name, audio_file, tag)
            elif image:
                f = self.transcode(image, audio_file, tag)
            else:
                raise ValueError("MP3 does not contain artwork and no "
                                 "image passed in on the command line")

            title, album, artist, year = (
                tag.get('TIT2', audio_file.split('/')[-1].replace('.mp3', '')),
                tag['TALB'].text, tag['TPE1'].text, tag['TDRC'].text
            )
            if isinstance(year, list):
                year = str(year[0])
            if isinstance(album, list):
                album = str(album[0])
            if isinstance(title, list):
                title = str(title[0])

            description = "From the album \"%s\" (%s).\n%s" % \
                          (album, year[0:4], self.description)

            self.upload(
                f.name,
                str(title),
                description,
                self.category,
                self.keywords,
            )
            f.close()


def main():
    parser = argparse.ArgumentParser(description='Create and upload YouTube videos from MP3s.')
    parser.add_argument('audio_files', type=str, nargs='+',
                        help='paths to the audio files to create video files from.')
    parser.add_argument('--image', dest='image',
                        help='path to the image to use as cover art')
    parser.add_argument('--category', dest='category', default="Music",
                        help='YouTube "category" parameter to put the videos into')
    parser.add_argument('--description', dest='description', default="",
                        help='YouTube description for all videos (- to read from stdin)')
    parser.add_argument('--keyword', dest='keywords', default=[], action="append",
                        help='keywords, each in their own separate --keyword arg')
    parser.add_argument('--make-public', dest='private', default=True, action="store_false",
                        help='automatically mark uploaded videos as public. '
                             'Default: private')
    parser.add_argument('--api-key-path', dest='api_key_path',
                        default=DEFAULT_API_KEY_PATH,
                        help='path to a YML file containing YouTube API keys')
    parser.add_argument('--google-access-token', dest='token', default=None,
                        help='pre-fetched Google oauth access token')
    args = parser.parse_args()

    me = MP3ToYoutube(**args.__dict__)
    me.process(args.audio_files)

if __name__ == "__main__":
    main()
