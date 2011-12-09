"""
Model definitions.

Note: some models are denormalized by design, this greatly simplifies (and speeds up) 
the queries necessary to fetch a certain entry.

"""
import os, random, hashlib, string, difflib

from django.db import models
from django.db import transaction
from django.contrib.auth.models import User, Group
from django.contrib import admin
from django.conf import settings

from mptt.models import MPTTModel, TreeForeignKey
from datetime import datetime, timedelta
from main.server import html, notegen

# import all constants
from main.server.const import *

import markdown

class UserProfile( models.Model ):
    """
    Stores user options

    >>> user, flag = User.objects.get_or_create(first_name='Jane', last_name='Doe', username='jane', email='jane')
    >>> prof = user.get_profile()
    """
    # mapping to the Django user
    user  = models.OneToOneField(User, unique=True, related_name='profile')
    
    # user chosen display nam
    display_name  = models.CharField(max_length=35, default='User', null=False,  db_index=True)
    
    # this designates a user as moderator
    type  = models.IntegerField(choices=USER_TYPES, default=USER_NORMAL)
    
    # globally unique id used to identify the user in a private feeds
    uuid = models.TextField(null=False,  db_index=True, unique=True)

    # this is the reputation
    score = models.IntegerField(default=0, blank=True)
    
    # denormalized badge fields to make rendering easier
    bronze_badges = models.IntegerField(default=0)
    silver_badges = models.IntegerField(default=0)
    gold_badges   = models.IntegerField(default=0)
    
    # the last visit by the user
    last_visited = models.DateTimeField(auto_now=True)
    
    # user status: active, suspended
    status = models.IntegerField(choices=USER_STATUS_TYPES, default=USER_ACTIVE)
    
    # description provided by the user as markup
    about_me = models.TextField(default="(about me)", null=True)

    # description provided by the user as html
    about_me_html = models.TextField(default="", null=True)
    
    # user provided location
    location = models.TextField(default="", null=True)
    
    # website may be used as a blog
    website  = models.URLField(default="", null=True, max_length=100)
    
    # this keeps track of whether we have already merged this user already
    # turned on only for the users that are migrated via the StackExchange import
    openid_merge  = models.BooleanField(default=True)
    
    @property
    def has_moderator_role(self):
        return (self.is_moderator or self.is_admin)
      
    @property
    def is_moderator(self):
        return self.type == USER_MODERATOR
    
    @property
    def is_admin(self):
        return self.type == USER_ADMIN
    
    @property
    def is_active(self):
        if self.suspended:
            return False
        if self.is_moderator or self.score >= settings.MINIMUM_REPUTATION:
            return True
        
        # right not we let it fall through to True
        # needs more throttles may go here here
        return True
    
    def get_absolute_url(self):
        return "/user/show/%i/" % self.user.id

    @property
    def note_count(self):
        note_count = Note.objects.filter(target=self.user).count()
        new_count  = Note.objects.filter(target=self.user, unread=True).count()
        return (note_count, new_count)
    
class Tag(models.Model):
    name  = models.TextField(max_length=50)
    count = models.IntegerField(default=0)
    
class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'count')

admin.site.register(Tag, TagAdmin)

class PostManager(models.Manager):
    ''' Used for all posts (question, answer, comment); returns only non-deleted posts '''
    def get_query_set(self):
        return super(PostManager, self).get_query_set().select_related('author','author__profile','children',).filter(type=POST_COMMENT)

class AnswerManager(models.Manager):
    ''' Used for all posts (question, answer, comment); returns only non-deleted posts '''
    def get_query_set(self):
        return super(AnswerManager, self).get_query_set().select_related('author','author__profile','children').filter(type=POST_ANSWER)

def post_write_auth(user, post, strict=True):
    """
    Authorizes writing to a post. Also sets the 'writable' attribute on the post
    Strict mode raises an immediate exception.
    """
     
    # authors or moderators may edit a post
    post.writable = (post.author == user) or user.profile.has_moderator_role
    
    if strict and not post.writeable:
        raise Exception('post write access denied')
        
    return post.writeable

def user_write_auth(target, user, strict=True):
    """
    Authorizes writing a target user by another user. Also sets the 'writable' attribute on the target.
    Moderatos may edit regular users, administrators may edit moderators and regular users.
    Strict mode raises an immediate exception.
    """
    
    if target.is_admin:
        # admins may not be moderated directly
        target.writable = False  
    elif user.is_admin:
        # admins may moderate everyone else
        target.writeable = True  
    elif target.is_moderator:
        # at this point only moderators are left
        target.writable = False        
    elif user.is_moderator :
        # at this point the target is a regular user
        target.writable = True
    elif target == user:
        # everyone may edit themselves
        target.writable = True
    else:
        # forbid access otherwise
        target.writeable = False
    
    if strict and not target.writeable:
        raise Exception('user write access denied')
        
    return target.writeable

class Post(models.Model):
    """
A post is content generated by a user
    """
    
    # the user that created the post
    author  = models.ForeignKey(User)
    content = models.TextField(null=False, blank=False) # the underlying Markdown
    html    = models.TextField(blank=True) # this is the sanitized HTML for display
    title   = models.TextField(blank=True, max_length=200)
    slug    = models.SlugField(blank=True, max_length=200)
    tag_val = models.CharField(max_length=200) # The tag value is the canonical form of the post's tags
    tag_set = models.ManyToManyField(Tag) # The tag set is built from the tag string and used only for fast filtering
    views = models.IntegerField(default=0, blank=True)
    score = models.IntegerField(default=0, blank=True)

    creation_date = models.DateTimeField(db_index=True)
    lastedit_date = models.DateTimeField()
    lastedit_user = models.ForeignKey(User, related_name='editor')
    
    # post status: active, closed, deleted 
    status = models.IntegerField(choices=POST_STATUS_TYPES, default=POST_OPEN)
    
    # the type of the post
    type = models.IntegerField(choices=POST_TYPES, db_index=True)
    
    # this will maintain the ancestor/descendant relationship bewteen posts
    root = models.ForeignKey('self', related_name="descendants", null=True, blank=True)
    
    # this will maintain parent-child replationships between posts
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children')
    
    # denormalized fields only that only apply to specific cases
    comment_count   = models.IntegerField(default=0, blank=True)
    answer_count    = models.IntegerField(default=0, blank=True)
    accepted        = models.BooleanField(default=False, blank=True)
   
    # this field will be used to allow posts to float back into relevance
    touch_date = models.DateTimeField(db_index=True, blank=True) 
    
    def get_absolute_url(self):
        return "/post/show/%i/" % self.id
          
    def set_tags(self):
        if self.type not in POST_CONTENT_ONLY:
            # save it so that we can set the many2many fiels
            self.tag_set.clear()
            tags = [ Tag.objects.get_or_create(name=name)[0] for name in self.get_tag_names() ]
            self.tag_set.add( *tags )
            self.save()
  
    def get_title(self):
        title = self.title
        if self.status == POST_DELETED:
            title = "%s [deleted ]" % self.title
        elif self.status == POST_CLOSED:
            title = "%s [closed]" % self.title
        return title
            
    def set_status(self):
        if self.answer_count == 0:
            self.status = POST_UNANSWERED
        elif self.answer_accepted:
            self.status = POST_ACCEPTED
        
    def get_vote(self, user, vote_type):
        if user.is_anonymous():
            return None
        try:
            return self.votes.get(author=user, type=vote_type)
        except Vote.DoesNotExist:
            return None
        
    def add_vote(self, user, vote_type):
        vote = Vote(author=user, type=vote_type, post=self)
        vote.save()
        return vote
        
    def remove_vote(self, user, vote_type):
        ''' Removes a vote from a user of a certain type if it exists
        Returns True if removed, False if it didn't exist'''
        vote = self.get_vote(user, vote_type)
        if vote:
            vote.delete()
            return True
        return False
        
    def get_tag_names(self):
        "Returns the post's tag values as a list of tag names"
        names = [ n.lower() for n in self.tag_val.split(' ') ]
        return map(unicode, names)
    
    def apply(self, dir):
        is_answer  = self.parent and self.type == POST_ANSWER
        is_comment = self.parent and self.type == POST_COMMENT
        if is_answer:
            self.parent.answer_count += dir
            self.parent.save()
        if is_comment:
            self.parent.comment_count += dir
            self.parent.save()
    
    def comments(self):
        objs = Post.objects.filter(parent=self, type=POST_COMMENT).select_related('author','author__profile')
        return objs
    
    def combine(self):
        "Returns a compact view that combines all parts of a post. Used in computing diffs between revisions"
        return "TITLE:%s\n%s\nTAGS:%s" % (self.title, self.content, self.tag_val)
        
    objects  = models.Manager()    
    answers  = AnswerManager()

class PostAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', )

admin.site.register(Post, PostAdmin)

class PostRevision(models.Model):
    """
    Represents various revisions of a single post
    """
    post    = models.ForeignKey(Post, related_name='revisions')
    diff    = models.TextField()    
    content = models.TextField()
    author  = models.ForeignKey(User)
    date    = models.DateTimeField(auto_now=True)
    
    def html(self):
        '''We won't cache the HTML in the DB because revisions are viewed fairly infrequently '''
        return html.generate(self.content)


@transaction.commit_on_success
def moderator_action(post, user, action, date=None):
    """
    Performs a moderator action on the post. Takes an action (one of REV_ACTIONS)
    and a user. Date is assumed to be now if not provided
    """
    # this will operate in strict mode an raise an exception
    if action == REV_CLOSE:
        post.status = POST_CLOSED       
    elif action == REV_DELETE:
        post.status = POST_DELETED
    else:
        post.set_status()
    post.save()
        
    text = notegen.post_moderator_action(user=user, post=post, action=action)
    send_note(target=post.author, sender=user, content=text,  type=NOTE_MODERATOR)
        
def send_note(sender, target, content, type, unread=True, date=None):
    "Sends a note to target"
    date = date or datetime.now()
    Note.objects.create(sender=sender, target=target, content=content, type=type, unread=unread, date=date)

@transaction.commit_on_success
def create_revision(post):
    "Creates a revision from a post. Called automatically via database signals"
      
    last = PostRevision.objects.filter(post=post).order_by('-date')[:1]
    content, author, date = post.combine(), post.lastedit_user, post.lastedit_date
    # compute the unified difference
    prev = last[0].content if last else ''
    if content != prev:
        diff = ''.join(difflib.unified_diff(prev.splitlines(1), content.splitlines(1)))
        rev  = PostRevision.objects.create(diff=diff, content=content, author=author, post=post, date=date)    

@transaction.commit_on_success
def post_create_notification(post):
    "Generates notifications to all users related with this post. Invoked only on the creation of the post"
    
    if post.root:
        authors = set()
        for child in Post.objects.filter(root=post):
            authors.add(child.author)
        text = notegen.post_action(user=post.author, post=post)
        
        for target in authors:
            unread = (target != post.author) # the undread flag will be off for the post author
            send_note(sender=self.author, target=target, content=text, type=NOTE_USER, unread=unread, date=self.creation_date)
        
class Note(models.Model):
    """
    Creates simple notifications that are active until the user deletes them
    """
    sender  = models.ForeignKey(User, related_name="note_sender") # the creator of the notification
    target  = models.ForeignKey(User, related_name="note_target", db_index=True) # the user that will get the note
    post    = models.ForeignKey(Post, related_name="note_post",null=True, blank=True) # the user that will get the note
    content = models.CharField(max_length=5000, default='') # this contains the raw message
    html    = models.CharField(max_length=5000, default='') # this contains the santizied content
    date    = models.DateTimeField(null=False)
    unread  = models.BooleanField(default=True)
    type    = models.IntegerField(choices=NOTE_TYPES, default=NOTE_USER)

    def get_absolute_url(self):
        return "/user/show/%s/" % self.target.id         

class Vote(models.Model):
    """
    >>> user, flag = User.objects.get_or_create(first_name='Jane', last_name='Doe', username='jane', email='jane')
    >>> post = Post.objects.create(author=user, type=POST_QUESTION, content='x')
    >>> vote = Vote(author=user, post=post, type=VOTE_UP)
    >>> vote.score()
    1
    """
    author = models.ForeignKey(User)
    post = models.ForeignKey(Post, related_name='votes')
    type = models.IntegerField(choices=VOTE_TYPES)
    
    def score(self):
        return POST_SCORE.get(self.type, 0)
    
    def reputation(self):
        return USER_REP.get(self.type, 0)
        
    def voter_reputation(self):
        return VOTER_REP.get(self.type, 0)
    
    def apply(self, dir=1):
        "Applies the score and reputation changes. Direction can be set to -1 to undo (ie delete vote)"
        if self.reputation():
            prof = self.post.author.get_profile()
            prof.score += dir * self.reputation()
            prof.save()
        
        if self.voter_reputation():
            prof = self.author.get_profile()
            prof.score += dir * self.voter_reputation()
            prof.save()

        if self.score():
            self.post.score += dir * self.score()
            self.post.save()
            
        if self.type == VOTE_ACCEPT:
            answer   = self.post
            question = self.post.parent
            if dir == 1:
                answer.post_accepted = True
                question.answer_accepted = True
            else:
                answer.post_accepted = False
                question.answer_accepted = False
            answer.save()
            #question.save()
            
           
class Badge(models.Model):
    name = models.CharField(max_length=50)
    description = models.CharField(max_length=200)
    type = models.IntegerField(choices=BADGE_TYPES)
    unique = models.BooleanField(default=False) # Unique badges may be earned only once
    secret = models.BooleanField(default=False) # Secret badges are not listed on the badge list
    count  = models.IntegerField(default=0) # Total number of times awarded
    
    def get_absolute_url(self):
        return "/badge/show/%s/" % self.id

class Award(models.Model):
    '''
    A badge being awarded to a user.Cannot be ManyToManyField
    because some may be earned multiple times
    '''
    badge = models.ForeignKey(Badge)
    user = models.ForeignKey(User)
    date = models.DateTimeField()
    
    def apply(self, dir=1):
        type = self.badge.type
        prof = self.user.get_profile()
        if type == BADGE_BRONZE:
            prof.bronze_badges += dir
        if type == BADGE_SILVER:
            prof.silver_badges += dir
        if type == BADGE_GOLD:
            prof.gold_badges += dir
        prof.save()
        self.badge.count += dir
        self.badge.save()
    
  
def apply_award(request, user, badge_name, messages=None):

    badge = Badge.objects.get(name=badge_name)
    award = Award.objects.filter(badge=badge, user=user)
    
    if award and badge.unique:
        # this badge has already been awarded
        return

    community = User.objects.get(username='community')
    award = Award.objects.create(badge=badge, user=user)
    text = notegen.badgenote(award.badge)
    note = Note.send(sender=community, target=user, content=text)
    if messages:
        messages.info(request, note.html)

# most of the site functionality, reputation change
# and voting is auto applied via database signals
#
# data migration will need to route through
# these models (this application) to ensure that all actions
# get applied properly
#
from django.db.models import signals

# Many models have apply() methods that need to be called when they are created
# and called with dir=-1 when deleted to update something.
MODELS_WITH_APPLY = [ Post, Vote, Award ]
    
def apply_instance(sender, instance, created, raw, *args, **kwargs):
    "Applies changes from an instance with an apply() method"
    if created and not raw: # Raw is true when importing from fixtures, in which case votes are already applied
        instance.apply(+1)

def unapply_instance(sender, instance,  *args, **kwargs):
    "Unapplies an instance when it is deleted"
    instance.apply(-1)
    
for model in MODELS_WITH_APPLY:
    signals.post_save.connect(apply_instance, sender=model)
    signals.post_delete.connect(unapply_instance, sender=model)

def make_uuid():
    "Returns a unique id"
    x = random.getrandbits(256)
    u = hashlib.md5(str(x)).hexdigest()
    return u

def create_profile(sender, instance, created, *args, **kwargs):
    "Post save hook for creating user profiles on user save"
    if created:
        uuid = make_uuid() 
        display_name = html.nuke(instance.get_full_name())
        UserProfile.objects.create(user=instance, uuid=uuid, display_name=display_name)

def update_profile(sender, instance, *args, **kwargs):
    "Pre save hook for profiles"
    instance.html = html.generate(instance.about_me)
    
from django.template.defaultfilters import slugify

def verify_post(sender, instance, *args, **kwargs):
    "Pre save post information that needs to be applied"
    
    if not hasattr(instance, 'lastedit_user'):
        instance.lastedit_user = instance.author
    
    if not instance.creation_date:
        instance.creation_date = datetime.now()
    
    if not instance.lastedit_date:
        instance.lastedit_date = datetime.now()
    
    # set the root automatically if not specified
    if not instance.root:
        instance.root = instance

    # set the title based on the parent    
    if not instance.title and instance.parent:
        instance.title = "%s: %s" % (instance.get_type_display, instance.parent.title)

    # generate a slug for the instance        
    instance.slug = slugify(instance.title)
        
    # generate the HTML from the content
    instance.html = html.generate(instance.content)
    
    # set the touch date
    instance.touch_date = datetime.now()
            
def finalize_post(sender, instance, created, *args, **kwargs):
    "Post save notice on a post, creates the revisions if necessary"
    if created:
        # when a new post is created users of descendants will be notified
        post_create_notification(instance)
    
    # create revisions for questions and answers
    if instance.type in ( (POST_QUESTION, POST_ANSWER) ):
        create_revision(instance)

def create_award(sender, instance, *args, **kwargs):
    "Pre save award function"
    if not instance.date:
        instance.date = datetime.now()

def create_note(sender, instance, *args, **kwargs):
    "Pre save notice function"
    if not instance.date:
        instance.date = datetime.now()
    instance.html = html.generate(instance.content)
  
def tags_changed(sender, instance, action, pk_set, *args, **kwargs):
    "Applies tag count updates upon post changes"
    if action == 'post_add':
        for pk in pk_set:
            tag = Tag.objects.get(pk=pk)
            tag.count += 1
            tag.save()
    if action == 'post_delete':
        for pk in pk_set:
            tag = Tag.objects.get(pk=pk)
            tag.count -= 1
            tag.save()
    if action == 'pre_clear': # Must be pre so we know what was cleared
        for tag in instance.tag_set.all():
            tag.count -= 1
            tag.save()
            
def tag_created(sender, instance, created, *args, **kwargs):
    "Zero out the count of a newly created Tag instance to avoid double counting in import"
    if created and instance.count != 0:
        # To avoid infinite recursion, we must disconnect the signal temporarily
        signals.post_save.disconnect(tag_created, sender=Tag)
        instance.count = 0
        instance.save()
        signals.post_save.connect(tag_created, sender=Tag)

# now connect all the signals
signals.post_save.connect( create_profile, sender=User )
signals.pre_save.connect( update_profile, sender=UserProfile )

signals.pre_save.connect( verify_post, sender=Post )
signals.post_save.connect( finalize_post, sender=Post )

signals.pre_save.connect( create_note, sender=Note )
signals.pre_save.connect( create_award, sender=Award )
signals.m2m_changed.connect( tags_changed, sender=Post.tag_set.through )
signals.post_save.connect( tag_created, sender=Tag )

# adding full text search capabilities

from whoosh import store, fields, index

WhooshSchema = fields.Schema(content=fields.TEXT(), pid=fields.NUMERIC(stored=True))

def create_index(sender=None, **kwargs):
    if not os.path.exists(settings.WHOOSH_INDEX):
        os.mkdir(settings.WHOOSH_INDEX)
        ix = index.create_in(settings.WHOOSH_INDEX, WhooshSchema)
        writer = ix.writer()

signals.post_syncdb.connect(create_index)

def update_index(sender, instance, created, **kwargs):
    
    ix = index.open_dir(settings.WHOOSH_INDEX)
    writer = ix.writer()

    if instance.type in POST_CONTENT_ONLY:
        text = instance.content
    else:
        text = instance.title + instance.content

    text = unicode(text)
    
    if created:                     
        writer.add_document(content=text, pid=instance.id)
        writer.commit()
    else:
        writer.update_document(content=text, pid=instance.id)        
        writer.commit()

def set_text_indexing(switch):
    if switch:
        signals.post_save.connect(update_index, sender=Post)
    else:
        signals.post_save.disconnect(update_index, sender=Post)

set_text_indexing(True)