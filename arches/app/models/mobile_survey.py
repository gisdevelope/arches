'''
ARCHES - a program developed to inventory and manage immovable cultural heritage.
Copyright (C) 2013 J. Paul Getty Trust and World Monuments Fund
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.
This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.
You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
'''

import uuid
import json
import couchdb
import urlparse
from copy import copy, deepcopy
from django.db import transaction
from arches.app.models import models
from arches.app.models.concept import Concept
from arches.app.models.tile import Tile
from arches.app.models.graph import Graph
from arches.app.models.models import ResourceInstance
from arches.app.models.system_settings import settings
from django.http import HttpRequest
from arches.app.utils.couch import Couch
from arches.app.utils.betterJSONSerializer import JSONSerializer, JSONDeserializer
import arches.app.views.search as search
from django.utils.translation import ugettext as _

class MobileSurvey(models.MobileSurveyModel):
    """
    Used for mapping complete mobile survey objects to and from the database
    """

    class Meta:
        proxy = True

    def __init__(self, *args, **kwargs):
        super(MobileSurvey, self).__init__(*args, **kwargs)
        # from models.MobileSurveyModel
        # self.id = models.UUIDField(primary_key=True, default=uuid.uuid1)
        # self.name = models.TextField(null=True)
        # self.active = models.BooleanField(default=False)
        # self.createdby = models.ForeignKey(User, related_name='createdby')
        # self.lasteditedby = models.ForeignKey(User, related_name='lasteditedby')
        # self.users = models.ManyToManyField(to=User, through='MobileSurveyXUser')
        # self.groups = models.ManyToManyField(to=Group, through='MobileSurveyXGroup')
        # self.startdate = models.DateField(blank=True, null=True)
        # self.enddate = models.DateField(blank=True, null=True)
        # self.description = models.TextField(null=True)
        # self.bounds = models.MultiPolygonField(null=True)
        # self.tilecache = models.TextField(null=True)
        # self.onlinebasemaps = JSONField(blank=True, null=True, db_column='onlinebasemaps')
        # self.datadownloadconfig = JSONField(blank=True, null=True, default='{"download":false, "count":1000, "resources":[]}')
        # end from models.MobileSurvey

        self.couch = Couch()

    def save(self):
        super(MobileSurvey, self).save()
        db = self.couch.create_db('project_' + str(self.id))
        # need to serialize to JSON and then back again because UUID's
        # cause the couch update_doc method to throw an error
        survey = JSONSerializer().serialize(self)
        survey = JSONDeserializer().deserialize(survey)
        survey['type'] = 'metadata'
        self.couch.update_doc(db, survey, 'metadata')
        self.load_data_into_couch()
        return db

    def delete(self):
        self.couch.delete_db('project_' + str(self.id))
        super(MobileSurvey, self).delete()

    def serialize(self, fields=None, exclude=None):
        """
        serialize to a different form than used by the internal class structure
        used to append additional values (like parent ontology properties) that
        internal objects (like models.Nodes) don't support
        """
        serializer = JSONSerializer()
        serializer.geom_format = 'geojson'
        obj = serializer.handle_model(self)
        ordered_cards = self.get_ordered_cards()
        ret = JSONSerializer().serializeToPython(obj)
        graphs = []
        graphids = []
        for card in self.cards.all():
            if card.graph_id not in graphids:
                graphids.append(card.graph_id)
                #we may want the full proxy model at some point, but for now just the root node color
                graph = Graph.objects.get(pk=card.graph_id)
                graph_obj = graph.serialize(exclude=['domain_connections', 'edges', 'relatable_resource_model_ids'])
                graph_obj['widgets'] = list(models.CardXNodeXWidget.objects.filter(card__graph=graph).distinct())
                for node in graph_obj['nodes']:
                    found = False
                    for widget in graph_obj['widgets']:
                        if node['nodeid'] == str(widget.node_id):
                            found = True
                            try:
                                collection_id = node['config']['rdmCollection']
                                concept_collection = Concept().get_child_collections_hierarchically(collection_id)
                                widget.config['options'] = concept_collection
                            except:
                                pass
                            break
                    if not found:
                        for card in graph_obj['cards']:
                            if card['nodegroup_id'] == node['nodegroup_id']:
                                widget = models.DDataType.objects.get(pk=node['datatype']).defaultwidget
                                if widget:
                                    widget_model = models.CardXNodeXWidget()
                                    widget_model.node_id = node['nodeid']
                                    widget_model.card_id = card['cardid']
                                    widget_model.widget_id = widget.pk
                                    widget_model.config = widget.defaultconfig
                                    try:
                                        collection_id = node['config']['rdmCollection']
                                        concept_collection = Concept().get_child_collections_hierarchically(collection_id)
                                        widget_model.config['options'] = concept_collection
                                    except:
                                        pass
                                    widget_model.label = node['name']
                                    graph_obj['widgets'].append(widget_model)
                                break
                graphs.append(graph_obj)

        ret['graphs'] = graphs
        ret['cards'] = ordered_cards
        try:
            ret['bounds'] = json.loads(ret['bounds'])
        except TypeError as e:
            print 'Could not parse', ret['bounds'], e

        return ret

    def get_ordered_cards(self):
        ordered_cards = models.MobileSurveyXCard.objects.filter(mobile_survey=self).order_by('sortorder')
        ordered_card_ids = [unicode(mpc.card_id) for mpc in ordered_cards]
        return ordered_card_ids

    def push_edits_to_db(self):
        # read all docs that have changes
        # save back to postgres db
        db = self.couch.create_db('project_' + str(self.id))
        ret = []
        with transaction.atomic():
            for row in self.couch.all_docs(db):
                ret.append(row)
                if row.doc['type'] == 'resource':
                    if row.doc['provisional_resource'] == 'true':
                        resourceinstance, created = ResourceInstance.objects.update_or_create(
                            resourceinstanceid = uuid.UUID(str(row.doc['resourceinstanceid'])),
                            defaults = {
                            'graph_id': uuid.UUID(str(row.doc['graph_id']))
                            }
                        )
                        print('Resource {0} Saved'.format(row.doc['resourceinstanceid']))

            for row in self.couch.all_docs(db):
                ret.append(row)
                if row.doc['type'] == 'tile':
                    if row.doc['provisionaledits'] is not None:
                        try:
                            tile = Tile.objects.get(tileid=row.doc['tileid'])
                            for user_edits in row.doc['provisionaledits'].items():
                                # user_edits is a dict with the user number as the key and data as the value
                                # {u'5': {
                                #     u'action': u'update',
                                #     u'reviewer': None,
                                #     u'reviewtimestamp': None,
                                #     u'status': u'review',
                                #     u'timestamp': u'2018-11-02T18:38:00.684250Z',
                                #     u'value': {
                                #         u'bb97b857-d646-11e8-9f94-acbc32b81a1b': u'Provisional String Value',
                                #         u'bb98aab8-d646-11e8-922d-acbc32b81a1b': None
                                #         }
                                #     }
                                # }
                                if tile.provisionaledits is None:
                                    tile.provisionaledits = {}
                                    tile.provisionaledits[user_edits[0]] = user_edits[1]
                                else:
                                    if user_edits[0] in tile.provisionaledits:
                                        if user_edits[1]['timestamp'] > tile.provisionaledits[user_edits[0]]['timestamp']:
                                            # If the mobile user edits are newer by UTC timestamp, overwrite the tile data
                                            tile.provisionaledits[user_edits[0]] = user_edits[1]

                            # If there are conflicting documents, lets clear those out
                            if '_conflicts' in row.doc:
                                for conflict_rev in row.doc['_conflicts']:
                                    conflict_data = db.get(row.id, rev=conflict_rev)
                                    for user_edits in conflict_data['provisionaledits'].items():
                                        if user_edits[0] in tile.provisionaledits:
                                            if user_edits[1]['timestamp'] > tile.provisionaledits[user_edits[0]]['timestamp']:
                                                # If the mobile user edits are newer by UTC timestamp, overwrite the tile data
                                                tile.provisionaledits[user_edits[0]] = user_edits[1]
                                    # Remove conflicted revision from couch
                                    db.delete(conflict_data)

                                # TODO: If user is provisional user, apply as provisional edit
                                # TODO: If user is reviewer, apply as authoritative edit
                        except Tile.DoesNotExist:
                            tile = Tile(row.doc)

                        tile.save()
                        tile_serialized = json.loads(JSONSerializer().serialize(tile))
                        tile_serialized['type'] = 'tile'
                        self.couch.update_doc(db, tile_serialized, tile_serialized['tileid'])
                        print('Tile {0} Saved'.format(row.doc['tileid']))
                        db.compact()
        return ret

    def collect_resource_instances_for_couch(self):
        """
        Uses the data definition configs of a mobile survey object to search for
        resource instances relevant to a mobile survey. Takes a user object which
        is required for search.
        """
        query = self.datadownloadconfig['custom']
        resource_types = self.datadownloadconfig['resources']
        instances = {}
        if query in ('', None) and len(resource_types) == 0:
            print "No resources or data query defined"
        else:
            request = HttpRequest()
            request.user = self.lasteditedby
            request.GET['mobiledownload'] = True
            if query in ('', None):
                if len(self.bounds.coords) == 0:
                    default_bounds = settings.DEFAULT_BOUNDS
                    default_bounds['features'][0]['properties']['inverted'] = False
                    request.GET['mapFilter'] = json.dumps(default_bounds)
                else:
                    request.GET['mapFilter'] = json.dumps({u'type': u'FeatureCollection', 'features':[{'geometry': json.loads(self.bounds.json)}]})
                request.GET['typeFilter'] = json.dumps([{'graphid': resourceid, 'inverted': False } for resourceid in self.datadownloadconfig['resources']])
            else:
                parsed = urlparse.urlparse(query)
                urlparams = urlparse.parse_qs(parsed.query)
                for k, v in urlparams.iteritems():
                    request.GET[k] = v[0]
            search_res_json = search.search_results(request)
            search_res = JSONDeserializer().deserialize(search_res_json.content)
            try:
                instances = {hit['_source']['resourceinstanceid']: hit['_source'] for hit in search_res['results']['hits']['hits']}
            except KeyError:
                print 'no instances found in', search_res
        return instances

    def load_tiles_into_couch(self, instances):
        """
        Takes a mobile survey object, a couch database instance, and a dictionary
        of resource instances to identify eligible tiles and load them into the
        database instance
        """
        db = self.couch.create_db('project_' + str(self.id))
        cards = self.cards.all()
        for card in cards:
            tiles = models.TileModel.objects.filter(nodegroup=card.nodegroup_id)
            tiles_serialized = json.loads(JSONSerializer().serialize(tiles))
            for tile in tiles_serialized:
                if str(tile['resourceinstance_id']) in instances:
                    try:
                        tile['type'] = 'tile'
                        self.couch.update_doc(db, tile, tile['tileid'])
                        # couch_record = db.get(tile['tileid'])
                        # if couch_record == None:
                        #     db[tile['tileid']] = tile
                        # else:
                        #     if couch_record['data'] != tile['data']:
                        #         couch_record['data'] = tile['data']
                        #         db[tile['tileid']] = couch_record
                    except Exception as e:
                        print e, tile

    def load_instances_into_couch(self, instances):
        """
        Takes a mobile survey object, a couch database instance, and a dictionary
        of resource instances and loads them into the database instance.
        """
        db = self.couch.create_db('project_' + str(self.id))
        for instanceid, instance in instances.iteritems():
            try:
                instance['type'] = 'resource'
                self.couch.update_doc(db, instance, instanceid)
            except Exception as e:
                print e, instance

    def load_data_into_couch(self):
        """
        Takes a mobile survey, a couch database intance and a django user and loads
        tile and resource instance data into the couch instance.
        """
        instances = self.collect_resource_instances_for_couch()
        self.load_tiles_into_couch(instances)
        self.load_instances_into_couch(instances)
