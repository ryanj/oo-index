# -*- coding: utf-8 -*-

import os, sys, re
import json
import requests
import github as PyGitHub
import dateutil.parser
import datetime

from flask import Flask, g, request, session, url_for, redirect, flash, render_template, send_from_directory
from flask_github import GitHub as AuthGitHub
from flask_funnel import Funnel

from collections import OrderedDict
from babel.dates import format_timedelta

class OOIndexError(Exception):
    '''OO-Index specific errors
    '''

app = Flask(__name__)
app.config.from_pyfile('indexapp.cfg')
Funnel(app)

## Jinja2 filters ########

@app.template_filter('owner_display')
def owner_display(quickstart):
    '''Given a `quickstart`, generate a link to quickstart owner.
    '''
    if quickstart.get('owner_avatar_url'):
        link = '<img alt="{qs[owner_name]}" src="{qs[owner_avatar_url]}" height="20" width="20">{qs[owner_name]}'.format(qs=quickstart)
    elif quickstart.get('owner_name'):
        link = quickstart['owner_name']
    else:
        link = quickstart['owner'].split('/')[-1]
    return '<a href="{qs[owner]}">{link}</a>'.format(qs=quickstart, link=link)

@app.template_filter('owner_name')
def owner_name(quickstart):
    '''Given a 'quickstart', return just the owner name
    '''
    if quickstart.get('owner_name'):
        return quickstart['owner_name']
    else:
        return quickstart['owner'].split('/')[-1]

@app.template_filter('git_repo_url')
def git_repo_url(quickstart):
    '''Given a 'quickstart', return the github repo url
    '''
    if quickstart.get('git_repo_url'):
        return quickstart['git_repo_url']
    else:
        return quickstart['owner'] + '/' + quickstart['name']

@app.template_filter('short_name')
def short_name(name):
    if name.lower() == 'quickstart':
        return 'QS'
    elif name.lower() == 'cartridge':
        return 'CART'
    else:
        return name

@app.template_filter('humanize_time')
def humanize_time(datestr):
    dt = dateutil.parser.parse(datestr)
    return format_timedelta(datetime.datetime.utcnow() - dt, locale='en_US') + ' ago'

## Quickstart file ########
class Quickstarts:
    '''Parse and cache content of file `quickstarts`.
    '''
    timestamp = 0
    cached = None

    def __init__(self):
        return self.load_data()

    def sync_data(self):
        index = []
 
        #read the current quickstart file
        for item in self.all():
            #refresh the stats for each record
            index.append(_read_quickstart_repo(item['owner'], item['name']))

        #write out an updated quickstart file
        self.save_data(index)
        return json.dumps(index)

    def save_data(self, data):
        qs_json = open(app.config['OO_INDEX_QUICKSTART_JSON_FULLPATH'], 'w')
        qs_json.write(data)
        qs_json.close()
        self.data = data
 
    def load_data(self):
        self.path = app.config['OO_INDEX_QUICKSTART_JSON_FULLPATH']
        self.data = Quickstarts.cached

        # Read file only if it has changed
        try:
            with open(self.path, 'r') as f:
                st = os.fstat(f.fileno())
                if Quickstarts.cached is not None and st.st_mtime == Quickstarts.timestamp:
                    return

                print >>sys.stderr, 'Refreshing' if Quickstarts.cached else 'Reading', self.path
                content = f.read()
                Quickstarts.timestamp = st.st_mtime
        except Exception, ex:
            print >>sys.stderr, "Error loading file %s: %s" % (self.path, ex)
            raise

        try:
            qstarts = json.loads(content, object_pairs_hook=OrderedDict)
            for qstart in qstarts:
                if qstart['type'].lower() == 'quickstart' and qstart.has_key('cartridges') and not qstart.has_key('launch_url'):
                    qstart['launch_url'] = make_launch_url(qstart['git_repo_url'], qstart['cartridges'], qstart['default_app_name'])
            self.data = Quickstarts.cached = qstarts
            
        except Exception, ex:
            print >>sys.stderr, "Error parsing file %s: %s" % (self.path, ex)
            raise

    def most_starred(self, count=10):
        return sorted(self.data, key=lambda x: int(x['stargazers']), reverse=True)[:count]

    def most_popular(self, count=10):
        return sorted(self.data, key=lambda x: int(x['watchers'] + x['stargazers'] + x['forks']), reverse=True)[:count]

    def latest(self, count=10):
        return sorted(self.data, key=lambda x: x['submitted_at'], reverse=True)[:count]

    def all(self, count=10):
        return self.data

class SearchEngine:
    ''' Super simple search engine for quickstarts json.
    '''

    def __init__(self):
        self._quickstarts = Quickstarts()
        self._all_quickstarts = self._quickstarts.all()

    def search(self, query):
        ''' TODO:
            ----
            Sample queries:
            1. jekyll
            2. jekyll mignev
            3. blog jekyll
            4. latest
               - latest quickstarts
            5. popular
               - most popular quickstarts
            6. popular ruby
               - most popular quickstarts that contains ruby as relevant word
            7. cartridge
               - this have to return all cartridges
        '''

        found_quickstarts = []

        query = query.split(' ')
        keywords_in_query = len(query)

        for quickstart in self._all_quickstarts:

            score = 0

            # I think that the first word you are searching for
            # is the most important
            importance_factor = keywords_in_query

            for keyword in query:
                # This is just a proof of concept implementation
                # have to research for searching algorithm
                if re.search(keyword, quickstart['name'], re.IGNORECASE):
                    score += (4 * importance_factor)

                if re.search(keyword, quickstart['owner'], re.IGNORECASE):
                    score += (3 * importance_factor)

                if re.search(keyword, quickstart['language'], re.IGNORECASE):
                    score += (2 * importance_factor)

                if re.search(keyword, quickstart['type'], re.IGNORECASE):
                    score += (1 * importance_factor)

                importance_factor -= 1

            if score > 0:
                found_quickstarts.append((score, quickstart))


        found_quickstarts = sorted(found_quickstarts, key=lambda quickstart: quickstart[0], reverse=True)
        result = [quickstart[1] for quickstart in found_quickstarts]
        return result


## authentication ##########

auth = AuthGitHub(app)

@app.before_request
def before_request():
    try:
        g.user = session['user']
    except KeyError:
        g.user = None

@app.route('/login')
def login():
    return auth.authorize(scope='public_repo')

@app.route('/login/callback')
@auth.authorized_handler
def authorized(token):
    next_url = request.args.get('next') or url_for('index')
    if token is None:
        return redirect(next_url)

    session['token'] = token
    session['user']  = auth.get('user')['login']
    return redirect(next_url)

@auth.access_token_getter
def token_getter():
    return session.get('token')

@app.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('token', None)
    return redirect(url_for('index'))

## views ############

@app.route('/')
def index():
    qs = Quickstarts()
    return render_template('index.html', most_starred=qs.most_starred(5), most_popular=qs.most_popular(5), latest=qs.latest(5))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/sync')
def sync():
    return _update_quickstart_data()

@app.route('/help')
def help():
    return render_template('help.html')

@app.route('/search', defaults = {'query': 'all'})
def search(query = "all"):
    serach_engine = SearchEngine()

    query = request.args.get('query', "")
    result = serach_engine.search(query)
    return render_template('search.html', quickstarts=result, query=query)

@app.route('/add', methods=['GET', 'POST'])
def add():
    if not g.user:
        return redirect(url_for('login'))

    pr = None
    form_data = {}
    if request.method == 'POST':
        try:
            form_data['type'] = request.form['type']
            form_data['github-username'] = request.form['github-username']
            form_data['github-repository'] = request.form['github-repository']
        except KeyError, ex:
            flash('Missing field: %s' % ex, 'error')
            return render_template('add.html') #, **form_data)
        form_data['app-name'] = request.form.get('app-name')
        form_data['cartridges'] = request.form.get('cartridges')

        try:
            pr = send_pull_request(form_data)
            flash('Pull Request created', 'info')
        except OOIndexError, ex:
            flash(str(ex), 'error')
        except PyGitHub.GithubException, ex:
            flash(ex.data.get('message', 'Unknown error.'), 'error')
        except Exception, ex:
            flash('%s: %s' % (ex.__class__, ex), 'error')

    return render_template('add.html', pr=pr) #, **form_data)

def _get_tree_element(repo, tree, path):
    for el in tree.tree:
        if el.path == path:
            return el
    raise OOIndexError('Invalid path "%s". Please contact support' % path)

def _read_github_file(username, reponame, filename):
    '''Fork repo and read content of `filename`.
    '''
    print 'Loading file content from %s/%s/%s' % (g.user, reponame, filename)
    gh = PyGitHub.Github(session['token'])
    user = gh.get_user()

    # Get user's oo-index repo, create if not exists
    try:
        repo = user.get_repo(reponame)
    except PyGitHub.UnknownObjectException:
        upstream = '%s/%s' % (username, reponame)
        print '  Forking project %s' % upstream
        user.create_fork(gh.get_repo(upstream))
        repo = None

        for i in range(10):
            try:
                repo = user.get_repo(reponame)
                break
            except PyGitHub.GithubException:
                print '  retry %i...' % i
                time.sleep(2)

        if not repo:
            msg = 'Timeout creating repository. Please try again later.'
            flash(msg, 'error')
            raise OOIndexError(msg)

    head = repo.get_commit('HEAD')
    tree = repo.get_git_tree(head.sha, recursive=True)
    blob = _get_tree_element(repo, tree, filename)
    content = requests.get(blob.url, headers={'Accept': 'application/vnd.github.v3.raw+json'}).json(object_pairs_hook=OrderedDict)

    return repo, head, tree, content

def _filter_repo_fields(repo):
    fields = [
        'description',
        'forks',
        'updated_at',
        'type',
        'owner',
        'id',
        'size',
        'watchers',
        'name',
        'language',
        'git_repo_url',
        'created_at',
        'default_app_name',
        'owner_type',
        'stargazers'
    ]

    r = OrderedDict([ (k,v) for k,v in repo.raw_data.items() if k in fields ])
    r['stargazers'] = repo.stargazers_count
    r['owner_type'] = repo.owner.type
    r['owner'] = repo.owner.html_url
    return r

def make_launch_url(repo_url, cartridges, app_name):
   cartstring = ''
   if len(cartridges) > 0:
       host = os.environ.get('OPENSHIFT_LAUNCH_URL', "https://openshift.redhat.com" )
       for cart in cartridges:
           cartstring += "&cartridges[]="+cart
   url = host+"/app/console/application_types/custom?name="+app_name+cartstring+"&initial_git_url="+repo_url
   return url

def _get_repo_for(username, reponame, token=None):
    if token:
        gh = PyGitHub.Github(token)
    else:
        gh = PyGitHub.Github()
    return gh.get_repo(username + '/' + reponame)

def _read_quickstart_repo(username, reponame):
    print 'Reading quickstart repo metadata %s/%s' % (username, reponame)
    return _filter_repo_fields(_get_repo_for(username, reponame))

def _update_quickstart_data():
    q = Quickstarts()
    return q.sync_data()

def send_pull_request(form_data):
    # read metadata of new quickstart repo
    qs_u = form_data['github-username']
    qs_r = form_data['github-repository']
    qs_n = form_data['app-name'] or qs_r
    qs_c = form_data['cartridges'] or ''
    qs_t = form_data['type'] or []
    try:
        qs = _read_quickstart_repo(qs_u, qs_r)
        if qs_n and len(qs_n) > 0 :
            qs['default_app_name'] = qs_n.replace('-','').replace(' ','')
        else:
            qs['default_app_name'] = qs_r.replace('-','').replace(' ','')
        qs['cartridges'] = qs_c.replace(' ','').split(',')
        qs['git_repo_url'] = "https://github.com/"+qs_u+"/"+qs_r+".git"
        qs['type'] = qs_t
        qs['submitted_at'] = datetime.datetime.isoformat(datetime.datetime.utcnow())
    except PyGitHub.UnknownObjectException:
        raise OOIndexError("Username or repository not found: %s/%s" % (qs_u, qs_r))

    try:
        owner = PyGitHub.Github().get_user(qs_u)
        qs['owner_name']       = owner.name
        qs['owner_avatar_url'] = owner.avatar_url
    except:
        qs['owner_name']       = qs['owner']
        qs['owner_avatar_url'] = ''

    # read content of original quickstart.json
    # fork repo if needed
    u = app.config['OO_INDEX_GITHUB_USERNAME']
    r = app.config['OO_INDEX_GITHUB_REPONAME']
    q = app.config['OO_INDEX_QUICKSTART_JSON']
    repo, head, tree, quickstart = _read_github_file(u, r, q)

    # add quickstart to quickstart.json
    quickstart.insert(0,qs)

    # create new blob with updated quickstart.json
    print "Creating blob...",; sys.stdout.flush()
    new_blob = repo.create_git_blob(json.dumps(quickstart, indent=3, encoding='utf-8'), 'utf-8')

    # create tree with new blob
    element = _get_tree_element(repo, tree, q)
    element = PyGitHub.InputGitTreeElement(path=element.path, mode=element.mode, type=element.type, sha=new_blob.sha)

    if not element:
        flash("File not found: %s/%s/%s" % (u, r, q), "error")
        return

    print "Updating tree...",; sys.stdout.flush()
    new_tree = repo.create_git_tree([ element ], tree)

    # create commit for new tree
    print "Creating commit...",; sys.stdout.flush()
    message = 'Quickstart add request: %s/%s' % (qs_u, qs_r)
    new_commit = repo.create_git_commit(message, new_tree, [ repo.get_git_commit(head.sha) ])

    # create new branch for new commit
    print "Creating branch...",; sys.stdout.flush()
    try:
        new_branch = repo.create_git_ref('refs/heads/%s-%s' % (qs_u, qs_r), new_commit.sha)
    except PyGitHub.UnknownObjectException:
        raise OOIndexError("Username or repository not found: %s/%s" % (qs_u, qs_r))

    # and finally, we send our pull request
    print "Creating pull request...",; sys.stdout.flush()
    upstream = _get_repo_for(u, r, session['token'])
    pr_params = {
        'title': message,
        'body': 'Automatically generated PR for oo-index',
        'base': 'master',
        'head': '%s:%s-%s' % (g.user, qs_u, qs_r),
    }
    pr = upstream.create_pull(**pr_params)
    return pr

##########################
if __name__ == "__main__":
    app.run(debug=app.debug)
