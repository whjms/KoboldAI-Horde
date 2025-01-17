import json, os, sys
from uuid import uuid4
from datetime import datetime
import threading, time
from logger import logger

class WaitingPrompt:
    # Every 10 secs we store usage data to disk
    def __init__(self, db, wps, pgs, prompt, user, models, params, **kwargs):
        self._db = db
        self._waiting_prompts = wps
        self._processing_generations = pgs
        self.prompt = prompt
        self.user = user
        self.models = models
        self.params = params
        self.n = params.get('n', 1)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.max_length = params.get("max_length", 80)
        self.max_content_length = params.get("max_content_length", 1024)
        self.total_usage = round(self.max_length * self.n / 1000000,2)
        self.id = str(uuid4())
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = params
        self.gen_payload["prompt"] = prompt
        # We always send only 1 iteration to KoboldAI
        self.gen_payload["n"] = 1
        # The generations that have been created already
        self.processing_gens = []
        self.last_process_time = datetime.now()
        self.servers = kwargs.get("servers", [])
        self.softprompts = kwargs.get("softprompts", [''])
        # Prompt requests are removed after 10 mins of inactivity, to prevent memory usage
        self.stale_time = 600


    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid server for it
        # Before we add it to the queue
        self._waiting_prompts.add_item(self)
        logger.info(f"New prompt request by user: {self.user.get_unique_alias()}")
        thread = threading.Thread(target=self.check_for_stale, args=())
        thread.daemon = True
        thread.start()

    # The mps still queued to be generated for this WP
    def get_queued_tokens(self):
        return(round(self.max_length * self.n,2))


    def needs_gen(self):
        if self.n > 0:
            return(True)
        return(False)

    def start_generation(self, server, matching_softprompt):
        if self.n <= 0:
            return
        new_gen = ProcessingGeneration(self, self._processing_generations, server)
        self.processing_gens.append(new_gen)
        self.n -= 1
        self.refresh()
        prompt_payload = {
            "payload": self.gen_payload,
            "softprompt": matching_softprompt,
            "id": new_gen.id,
        }
        return(prompt_payload)

    def is_completed(self):
        if self.needs_gen():
            return(False)
        for procgen in self.processing_gens:
            if not procgen.is_completed():
                return(False)
        return(True)

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
        }
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["finished"] += 1
            else:
                ret_dict["processing"] += 1
        return(ret_dict)

    def get_status(self, lite = False):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        ret_dict["done"] = self.is_completed()
        ret_dict["generations"] = []
        queue_pos, queued_tokens, queued_n = self.get_own_queue_stats()
        # We increment the priority by 1, because it starts at 0
        # This means when all our requests are currently processing or done, with nothing else in the queue, we'll show queue position 0 which is appropriate.
        ret_dict["queue_position"] = queue_pos + 1
        active_servers = self._db.count_active_servers()
        # If there's less requests than the number of active servers
        # Then we need to adjust the parallelization accordingly
        if queued_n < active_servers:
            active_servers = queued_n
        avg_token_per_sec = (self._db.stats.get_request_avg()) * active_servers
        # Is this is 0, it means one of two things:
        # 1. This horde hasn't had any requests yet. So we'll initiate it to 1mpss
        # 2. All gens for this WP are being currently processed, so we'll just set it to 1 to avoid a div by zero, but it's not used anyway as it will just divide 0/1
        if avg_token_per_sec == 0:
            avg_token_per_sec = 1
        wait_time = queued_tokens / avg_token_per_sec
        # We add the expected running time of our processing gens
        for procgen in self.processing_gens:
            wait_time += procgen.get_expected_time_left()
        ret_dict["wait_time"] = round(wait_time)
        if not lite:
            for procgen in self.processing_gens:
                if procgen.is_completed():
                    gen_dict = {
                        "text": procgen.generation,
                        "server_id": procgen.server.id,
                        "server_name": procgen.server.name,
                    }
                    ret_dict["generations"].append(gen_dict)
        return(ret_dict)


    # Same as status, but without the images to avoid unnecessary size
    def get_lite_status(self):
        ret_dict = self.get_status(True)
        return(ret_dict)

    # Get out position in the working prompts queue sorted by kudos
    # If this gen is completed, we return (-1,-1) which represents this, to avoid doing operations.
    def get_own_queue_stats(self):
        if self.needs_gen():
            return(self._waiting_prompts.get_wp_queue_stats(self))
        return(-1,0,0)

    def record_usage(self, tokens, kudos):
        self.user.record_usage(tokens, kudos)
        self.refresh()

    def check_for_stale(self):
        while True:
            if self.is_stale():
                self.delete()
                break
            time.sleep(600)

    def delete(self):
        for gen in self.processing_gens:
            gen.delete()
        self._waiting_prompts.del_item(self)
        del self

    def refresh(self):
        self.last_process_time = datetime.now()

    def is_stale(self):
        if (datetime.now() - self.last_process_time).seconds > self.stale_time:
            return(True)
        return(False)


class ProcessingGeneration:
    def __init__(self, owner, pgs, server):
        self._processing_generations = pgs
        self.id = str(uuid4())
        self.owner = owner
        self.server = server
        # We store the model explicitly, in case the server changed models between generations
        self.model = server.model
        self.generation = None
        self.kudos = 0
        self.start_time = datetime.now()
        self._processing_generations.add_item(self)

    def set_generation(self, generation):
        if self.is_completed():
            return(0)
        self.generation = generation
        tokens = self.owner.max_length
        self.kudos = self.owner._db.convert_tokens_to_kudos(tokens, self.model)
        tokens_per_sec = self.owner._db.stats.record_fulfilment(tokens,self.start_time)
        self.server.record_contribution(tokens, self.kudos, tokens_per_sec)
        self.owner.record_usage(tokens, self.kudos)
        logger.info(f"New Generation worth {self.kudos} kudos, delivered by server: {self.server.name}")
        return(self.kudos)

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)

    def delete(self):
        self._processing_generations.del_item(self)
        del self

    def get_expected_time_left(self):
        if self.is_completed():
            return(0)
        seconds_needed = self.owner.max_length / self.server.get_performance_average()
        seconds_elapsed = (datetime.now() - self.start_time).seconds
        expected_time = seconds_needed - seconds_elapsed
        # In case we run into a slow request
        if expected_time < 0:
            expected_time = 0
        return(expected_time)


class KAIServer:
    def __init__(self, db):
        self._db = db
        self.kudos_details = {
            "generated": 0,
            "uptime": 0,
        }
        self.last_reward_uptime = 0
        # Every how many seconds does this server get a kudos reward
        self.uptime_reward_threshold = 600

    def create(self, user, name, softprompts):
        self.user = user
        self.name = name
        self.softprompts = softprompts
        self.id = str(uuid4())
        self.contributions = 0
        self.fulfilments = 0
        self.kudos = 0
        self.performances = []
        self.uptime = 0
        self._db.register_new_server(self)

    def check_in(self, model, max_length, max_content_length, softprompts):
        if not self.is_stale():
            self.uptime += (datetime.now() - self.last_check_in).seconds
            # Every 10 minutes of uptime gets kudos rewarded
            if self.uptime - self.last_reward_uptime > self.uptime_reward_threshold:
                # Bigger model uptime gets more kudos
                kudos = round(self._db.stats.calculate_model_multiplier(model) / 2.75, 2)
                self.modify_kudos(kudos,'uptime')
                self.user.record_uptime(kudos)
                logger.debug(f"server '{self.name}' received {kudos} kudos for uptime of {self.uptime_reward_threshold} seconds.")
                self.last_reward_uptime = self.uptime
        else:
            # If the server comes back from being stale, we just reset their last_reward_uptime
            # So that they have to stay up at least 10 mins to get uptime kudos
            self.last_reward_uptime = self.uptime
        self.last_check_in = datetime.now()
        self.model = model
        self.max_content_length = max_content_length
        self.max_length = max_length
        self.softprompts = softprompts

    def get_human_readable_uptime(self):
        if self.uptime < 60:
            return(f"{self.uptime} seconds")
        elif self.uptime < 60*60:
            return(f"{round(self.uptime/60,2)} minutes")
        elif self.uptime < 60*60*24:
            return(f"{round(self.uptime/60/60,2)} hours")
        else:
            return(f"{round(self.uptime/60/60/24,2)} days")

    def can_generate(self, waiting_prompt):
        # takes as an argument a WaitingPrompt class and checks if this server is valid for generating it
        is_matching = True
        skipped_reason = None
        if len(waiting_prompt.servers) >= 1 and self.id not in waiting_prompt.servers:
            is_matching = False
            skipped_reason = 'server_id'
        if len(waiting_prompt.models) >= 1 and self.model not in waiting_prompt.models:
            is_matching = False
            skipped_reason = 'models'
        if self.max_content_length < waiting_prompt.max_content_length:
            is_matching = False
            skipped_reason = 'max_content_length'
        if self.max_length < waiting_prompt.max_length:
            is_matching = False
            skipped_reason = 'max_length'
        matching_softprompt = False
        for sp in waiting_prompt.softprompts:
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if sp == '':
                matching_softprompt = True
                break
            for sp_name in self.softprompts:
                if sp in sp_name:
                    matching_softprompt = True
                    break
        if not matching_softprompt:
            is_matching = False
            skipped_reason = 'matching_softprompt'
        return([is_matching,skipped_reason])

    def record_contribution(self, tokens, kudos, tokens_per_sec):
        self.user.record_contributions(tokens, kudos)
        self.modify_kudos(kudos,'generated')
        self.contributions += tokens
        self.fulfilments += 1
        self.performances.append(tokens_per_sec)
        if len(self.performances) > 20:
            del self.performances[0]

    def modify_kudos(self, kudos, action = 'generated'):
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + abs(kudos), 2) 

    def get_performance_average(self):
        if len(self.performances):
            ret_num = sum(self.performances) / len(self.performances)
        else:
            # Always sending at least 1 pixelstep per second, to avoid divisions by zero
            ret_num = 1
        return(ret_num)

    def get_performance(self):
        if len(self.performances):
            ret_str = f'{round(sum(self.performances) / len(self.performances),1)} tokens per second'
        else:
            ret_str = f'No requests fulfilled yet'
        return(ret_str)

    def is_stale(self):
        try:
            if (datetime.now() - self.last_check_in).seconds > 300:
                return(True)
        # If the last_check_in isn't set, it's a new server, so it's stale by default
        except AttributeError:
            return(True)
        return(False)

    def serialize(self):
        ret_dict = {
            "oauth_id": self.user.oauth_id,
            "name": self.name,
            "model": self.model,
            "max_length": self.max_length,
            "max_content_length": self.max_content_length,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details,
            "performances": self.performances,
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "softprompts": self.softprompts,
            "uptime": self.uptime,
        }
        return(ret_dict)

    def deserialize(self, saved_dict, convert_flag = None):
        self.user = self._db.find_user_by_oauth_id(saved_dict["oauth_id"])
        self.name = saved_dict["name"]
        self.model = saved_dict["model"]
        self.max_length = saved_dict["max_length"]
        self.max_content_length = saved_dict["max_content_length"]
        self.contributions = saved_dict["contributions"]
        if convert_flag == "to_tokens":
            self.contributions = round(saved_dict["contributions"] / 4)
        self.fulfilments = saved_dict["fulfilments"]
        self.kudos = saved_dict.get("kudos",0)
        self.kudos_details = saved_dict.get("kudos_details",self.kudos_details)
        self.performances = saved_dict.get("performances",[])
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.softprompts = saved_dict.get("softprompts",[])
        self.uptime = saved_dict.get("uptime",0)
        self._db.servers[self.name] = self

class Index:
    def __init__(self):
        self._index = {}

    def add_item(self, item):
        self._index[item.id] = item

    def get_item(self, uuid):
        return(self._index.get(uuid))

    def del_item(self, item):
        del self._index[item.id]

    def get_all(self):
        return(self._index.values())


class PromptsIndex(Index):

    def count_waiting_requests(self, user):
        count = 0
        for wp in self._index.values():
            if wp.user == user and not wp.is_completed():
                count += 1
        return(count)

    def count_totals(self):
        ret_dict = {
            "queued_requests": 0,
            "queued_tokens": 0,
        }
        for wp in self._index.values():
            ret_dict["queued_requests"] += wp.n
            if wp.n > 0:
                ret_dict["queued_tokens"] += wp.max_length
        return(ret_dict)


    def get_waiting_wp_by_kudos(self):
        sorted_wp_list = sorted(self._index.values(), key=lambda x: x.user.kudos, reverse=True)
        final_wp_list = []
        for wp in sorted_wp_list:
            if wp.needs_gen():
                final_wp_list.append(wp)
        return(final_wp_list)

    # Returns the queue position of the provided WP based on kudos
    # Also returns the amount of mps until the wp is generated
    # Also returns the amount of different gens queued
    def get_wp_queue_stats(self, wp):
        tokens_ahead_in_queue = 0
        n_ahead_in_queue = 0
        priority_sorted_list = self.get_waiting_wp_by_kudos()
        for iter in range(len(priority_sorted_list)):
            tokens_ahead_in_queue += priority_sorted_list[iter].get_queued_tokens()
            n_ahead_in_queue += priority_sorted_list[iter].n
            if priority_sorted_list[iter] == wp:
                return(iter, tokens_ahead_in_queue, n_ahead_in_queue)
        # -1 means the WP is done and not in the queue
        return(-1,0,0)


class GenerationsIndex(Index):
    pass

class User:
    def __init__(self, db):
        self._db = db
        self.kudos = 0
        self.kudos_details = {
            "accumulated": 0,
            "gifted": 0,
            "received": 0,
        }
        self.max_concurrent_wps = 2

    def create_anon(self):
        self.username = 'Anonymous'
        self.oauth_id = 'anon'
        self.api_key = '0000000000'
        self.invite_id = ''
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.id = 0
        self.contributions = {
            "tokens": 0,
            "fulfillments": 0
        }
        self.usage = {
            "tokens": 0,
            "requests": 0
        }
        # We allow anonymous users more leeway for the max amount of concurrent requests
        # This is balanced by their lower priority
        self.max_concurrent_wps = 30

    def create(self, username, oauth_id, api_key, invite_id):
        self.username = username
        self.oauth_id = oauth_id
        self.api_key = api_key
        self.invite_id = invite_id
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.id = self._db.register_new_user(self)
        self.contributions = {
            "tokens": 0,
            "fulfillments": 0
        }
        self.usage = {
            "tokens": 0,
            "requests": 0
        }

    # Checks that this user matches the specified API key
    def check_key(api_key):
        if self.api_key and self.api_key == api_key:
            return(True)
        return(False)

    def get_unique_alias(self):
        return(f"{self.username}#{self.id}")

    def record_usage(self, tokens, kudos):
        self.usage["tokens"] += tokens
        self.usage["requests"] += 1
        self.modify_kudos(-kudos,"accumulated")

    def record_contributions(self, tokens, kudos):
        self.contributions["tokens"] += tokens
        self.contributions["fulfillments"] += 1
        self.modify_kudos(kudos,"accumulated")

    def record_uptime(self, kudos):
        self.modify_kudos(kudos,"accumulated")

    def modify_kudos(self, kudos, action = 'accumulated'):
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + kudos, 2)


    def serialize(self):
        ret_dict = {
            "username": self.username,
            "oauth_id": self.oauth_id,
            "api_key": self.api_key,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details,
            "id": self.id,
            "invite_id": self.invite_id,
            "contributions": self.contributions,
            "usage": self.usage,
            "max_concurrent_wps": self.max_concurrent_wps,
            "creation_date": self.creation_date.strftime("%Y-%m-%d %H:%M:%S"),
            "last_active": self.last_active.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return(ret_dict)

    def deserialize(self, saved_dict, convert_flag = None):
        self.username = saved_dict["username"]
        self.oauth_id = saved_dict["oauth_id"]
        self.api_key = saved_dict["api_key"]
        self.kudos = saved_dict["kudos"]
        self.kudos_details = saved_dict.get("kudos_details", self.kudos_details)
        self.id = saved_dict["id"]
        self.invite_id = saved_dict["invite_id"]
        self.contributions = saved_dict["contributions"]
        if convert_flag == "to_tokens" and "chars" in self.contributions:
            self.contributions["tokens"] = round(self.contributions["chars"] / 4)
            del self.contributions["chars"]
        self.usage = saved_dict["usage"]
        if convert_flag == "to_tokens" and "chars" in self.usage:
            self.usage["tokens"] = round(self.usage["chars"] / 4)
            del self.usage["chars"]
        self.max_concurrent_wps = saved_dict.get("max_concurrent_wps", 2)
        if self.api_key == '0000000000':
            self.max_concurrent_wps = 30
        self.creation_date = datetime.strptime(saved_dict["creation_date"],"%Y-%m-%d %H:%M:%S")
        self.last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S")


class Stats:
    def __init__(self, db, convert_flag = None, interval = 60):
        self.db = db
        self.server_performances = []
        self.model_mulitpliers = {}
        self.fulfillments = []
        self.interval = interval
        self.last_pruning = datetime.now()


    def record_fulfilment(self, tokens, starting_time):
        seconds_taken = (datetime.now() - starting_time).seconds
        if seconds_taken == 0:
            tokens_per_sec = 1
        else:
            tokens_per_sec = round(tokens / seconds_taken,1)
        if len(self.server_performances) >= 10:
            del self.server_performances[0]
        self.server_performances.append(tokens_per_sec)
        fulfillment_dict = {
            "tokens": tokens,
            "start_time": starting_time,
            "deliver_time": datetime.now(),
        }
        self.fulfillments.append(fulfillment_dict)
        return(tokens_per_sec)

    def get_kilotokens_per_min(self):
        total_tokens = 0
        pruned_array = []
        for fulfillment in self.fulfillments.copy():
            if (datetime.now() - fulfillment["deliver_time"]).seconds <= 60:
                pruned_array.append(fulfillment)
                total_tokens += fulfillment["tokens"]
                # logger.debug([(datetime.now() - fulfillment["deliver_time"]).seconds, total_tokens])
        # To avoid race condition, we do it all in the same place, instead of using a thread
        if (datetime.now() - self.last_pruning).seconds > self.interval:
            self.last_pruning = datetime.now()
            self.fulfillments = pruned_array
            logger.debug("Pruned fulfillments")
        kilotokens_per_min = round(total_tokens / 1000,2)
        return(kilotokens_per_min)

    def calculate_model_multiplier(self, model_name):
        # To avoid doing this calculations all the time
        multiplier = self.model_mulitpliers.get(model_name)
        if multiplier:
            return(multiplier)
        try:
            import transformers, accelerate
            config = transformers.AutoConfig.from_pretrained(model_name)
            with accelerate.init_empty_weights():
                model = transformers.AutoModelForCausalLM.from_config(config)
            params_sum = sum(v.numel() for v in model.state_dict().values())
            logger.info(f"New Model {model_name} parameter = {params_sum}")
            multiplier = params_sum / 1000000000
        except OSError:
            logger.error(f"Model '{model_name}' not found in hugging face. Defaulting to multiplier of 1.")
            multiplier = 1
        self.model_mulitpliers[model_name] = multiplier
        return(multiplier)

    def get_request_avg(self):
        if len(self.server_performances) == 0:
            return(0)
        avg = sum(self.server_performances) / len(self.server_performances)
        return(round(avg,1))

    @logger.catch
    def serialize(self):
        serialized_fulfillments = []
        for fulfillment in self.fulfillments:
            json_fulfillment = {
                "tokens": fulfillment["tokens"],
                "start_time": fulfillment["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "deliver_time": fulfillment["deliver_time"].strftime("%Y-%m-%d %H:%M:%S"),
            }
            serialized_fulfillments.append(json_fulfillment)
        ret_dict = {
            "server_performances": self.server_performances,
            "model_mulitpliers": self.model_mulitpliers,
            "fulfillments": serialized_fulfillments,
        }
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        # Convert old key
        if "fulfilment_times" in saved_dict:
            self.server_performances = saved_dict["fulfilment_times"]
        else:
            self.server_performances = saved_dict["server_performances"]
        deserialized_fulfillments = []
        for fulfillment in saved_dict.get("fulfillments", []):
            if convert_flag == "to_tokens":
                fulfillment["tokens"] = round(fulfillment["chars"] / 4)
            class_fulfillment = {
                "tokens": fulfillment["tokens"],
                "start_time": datetime.strptime(fulfillment["start_time"],"%Y-%m-%d %H:%M:%S"),
                "deliver_time":datetime.strptime( fulfillment["deliver_time"],"%Y-%m-%d %H:%M:%S"),
            }
            deserialized_fulfillments.append(class_fulfillment)
        self.model_mulitpliers = saved_dict["model_mulitpliers"]
        self.fulfillments = deserialized_fulfillments
    

class Database:
    def __init__(self, convert_flag = None, interval = 3):
        self.interval = interval
        self.ALLOW_ANONYMOUS = True
        # This is used for synchronous generations
        self.SERVERS_FILE = "db/servers.json"
        self.servers = {}
        # Other miscellaneous statistics
        self.STATS_FILE = "db/stats.json"
        self.stats = Stats(self)
        self.USERS_FILE = "db/users.json"
        self.users = {}
        # Increments any time a new user is added
        # Is appended to usernames, to ensure usernames never conflict
        self.last_user_id = 0
        logger.init(f"Database Load", status="Starting")
        if convert_flag:
            logger.init_warn(f"Convert Flag '{convert_flag}' received.", status="Converting")
        if os.path.isfile(self.USERS_FILE):
            with open(self.USERS_FILE) as db:
                serialized_users = json.load(db)
                for user_dict in serialized_users:
                    new_user = User(self)
                    new_user.deserialize(user_dict,convert_flag)
                    self.users[new_user.oauth_id] = new_user
                    if new_user.id > self.last_user_id:
                        self.last_user_id = new_user.id
        self.anon = self.find_user_by_oauth_id('anon')
        if not self.anon:
            self.anon = User(self)
            self.anon.create_anon()
            self.users[self.anon.oauth_id] = self.anon
        if os.path.isfile(self.SERVERS_FILE):
            with open(self.SERVERS_FILE) as db:
                serialized_servers = json.load(db)
                for server_dict in serialized_servers:
                    new_server = KAIServer(self)
                    new_server.deserialize(server_dict,convert_flag)
                    self.servers[new_server.name] = new_server
        if os.path.isfile(self.STATS_FILE):
            with open(self.STATS_FILE) as stats_db:
                self.stats.deserialize(json.load(stats_db),convert_flag)

        if convert_flag:
            self.write_files_to_disk()
            logger.init_ok(f"Convertion complete.", status="Exiting")
            sys.exit()
        thread = threading.Thread(target=self.write_files, args=())
        thread.daemon = True
        thread.start()
        logger.init_ok(f"Database Load", status="Completed")

    def write_files(self):
        logger.init_ok("Database Store Thread", status="Started")
        while True:
            self.write_files_to_disk()
            time.sleep(self.interval)

    def write_files_to_disk(self):
        if not os.path.exists('db'):
            os.mkdir('db')
        server_serialized_list = []
        for server in self.servers.values():
            # We don't store data for anon servers
            if server.user == self.anon: continue
            server_serialized_list.append(server.serialize())
        with open(self.SERVERS_FILE, 'w') as db:
            json.dump(server_serialized_list,db)
        with open(self.STATS_FILE, 'w') as db:
            json.dump(self.stats.serialize(),db)
        user_serialized_list = []
        for user in self.users.values():
            user_serialized_list.append(user.serialize())
        with open(self.USERS_FILE, 'w') as db:
            json.dump(user_serialized_list,db)

    def get_top_contributor(self):
        top_contribution = 0
        top_contributor = None
        user = None
        for user in self.users.values():
            if user.contributions['tokens'] > top_contribution and user != self.anon:
                top_contributor = user
                top_contribution = user.contributions['tokens']
        return(top_contributor)

    def get_top_server(self):
        top_server = None
        top_server_contribution = 0
        for server in self.servers:
            if self.servers[server].contributions > top_server_contribution:
                top_server = self.servers[server]
                top_server_contribution = self.servers[server].contributions
        return(top_server)

    def get_available_models(self):
        models_ret = {}
        for server in self.servers.values():
            if server.is_stale():
                continue
            models_ret[server.model] = models_ret.get(server.model,0) + 1
        return(models_ret)

    def count_active_servers(self):
        count = 0
        for server in self.servers.values():
            if not server.is_stale():
                count += 1
        return(count)

    def get_total_usage(self):
        totals = {
            "tokens": 0,
            "fulfilments": 0,
        }
        for server in self.servers.values():
            totals["tokens"] += server.contributions
            totals["fulfilments"] += server.fulfilments
        return(totals)

    def register_new_user(self, user):
        self.last_user_id += 1
        self.users[user.oauth_id] = user
        logger.info(f'New user created: {user.username}#{self.last_user_id}')
        return(self.last_user_id)

    def register_new_server(self, server):
        self.servers[server.name] = server
        logger.info(f'New server checked-in: {server.name} by {server.user.get_unique_alias()}')

    def find_user_by_oauth_id(self,oauth_id):
        if oauth_id == 'anon' and not self.ALLOW_ANONYMOUS:
            return(None)
        return(self.users.get(oauth_id))

    def find_user_by_username(self, username):
        for user in self.users.values():
            uniq_username = username.split('#')
            if user.username == uniq_username[0] and user.id == int(uniq_username[1]):
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_user_by_api_key(self,api_key):
        for user in self.users.values():
            if user.api_key == api_key:
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_server_by_name(self,server_name):
        return(self.servers.get(server_name))

    def transfer_kudos(self, source_user, dest_user, amount):
        if amount > source_user.kudos:
            return([0,'Not enough kudos.'])
        source_user.modify_kudos(-amount, 'gifted')
        dest_user.modify_kudos(amount, 'received')
        return([amount,'OK'])

    def transfer_kudos_to_username(self, source_user, dest_username, amount):
        dest_user = self.find_user_by_username(dest_username)
        if not dest_user:
            return([0,'Invalid target username.'])
        if dest_user == self.anon:
            return([0,'Tried to burn kudos via sending to Anonymous. Assuming PEBKAC and aborting.'])
        if dest_user == source_user:
            return([0,'Cannot send kudos to yourself, ya monkey!'])
        kudos = self.transfer_kudos(source_user,dest_user, amount)
        return(kudos)

    def transfer_kudos_from_apikey_to_username(self, source_api_key, dest_username, amount):
        source_user = self.find_user_by_api_key(source_api_key)
        if not source_user:
            return([0,'Invalid API Key.'])
        if source_user == self.anon:
            return([0,'You cannot transfer Kudos from Anonymous, smart-ass.'])
        kudos = self.transfer_kudos_to_username(source_user, dest_username, amount)
        return(kudos)

    def convert_tokens_to_kudos(self, tokens, model_name):
        multiplier = self.stats.calculate_model_multiplier(model_name)
        # We want a 2.7B model at 80 tokens to be worth around 10 kudos
        kudos = round(tokens * multiplier / 21, 2)
        # logger.info([tokens,multiplier,kudos])
        return(kudos)

