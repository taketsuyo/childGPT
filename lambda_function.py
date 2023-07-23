# -*- coding: utf-8 -*-

# This sample demonstrates handling intents from an Alexa skill using the Alexa Skills Kit SDK for Python.
# Please visit https://alexa.design/cookbook for additional examples on implementing slots, dialog management,
# session persistence, api calls, and more.
# This sample is built using the handler classes approach in skill builder.
import logging
import ask_sdk_core.utils as ask_utils
import os
import time
import datetime
import openai
openai.api_key = os.environ['API_Key']

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from ratelimiter import RateLimiter
# ユーザーごとのレートリミッターを保存するディクショナリ
user_rate_limiters = {}

conversation_history = []

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.dispatch_components import AbstractExceptionHandler
from ask_sdk_core.handler_input import HandlerInput

from ask_sdk_model import Response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class DynamoDBRateLimiter:

    def __init__(self, table_name, question_table_name):
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(table_name)
        self.question_table = self.dynamodb.Table(question_table_name)

    def handle(self, handler_input):
        user_id = handler_input.request_envelope.context.system.user.user_id

        item = self.get_item(user_id)
        
        if item is None:
            self.reset_count(user_id)
            return True

        if float(time.time()) - float(item['last_request_time']) > 36000: #10時間当たり（3600秒あたり）を
            self.reset_daily_count(user_id)
            return True
        else:
            count = item['api_calls']

        if count < 1000:  # 10時間あたり1000回まで
            self.increment_count(user_id)
            return True
        else:
            return False  # 10時間あたり30回を超えたら、Falseを返す


    def record_question(self, user_id, question):
        try:
            response = self.question_table.put_item(
                Item={
                    'user_id': user_id,  # Partition key
                    'asked_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S:%f'),  # Sort key
                    'question': question,
                }
            )
        except ClientError as e:
            print(e.response['Error']['Message'])
        else:
            return response


    def increment_count(self, user_id):
        try:
            response = self.table.update_item(
                Key={'user_id': user_id},
                UpdateExpression="set api_calls = api_calls + :val, last_request_time = :time, total_api_calls = total_api_calls + :val, daily_api_calls = daily_api_calls + :val",
                ExpressionAttributeValues={
                    ':val': 1,
                    ':time': int(time.time())
                },
                ReturnValues="UPDATED_NEW"
            )
        except ClientError as e:
            if e.response['Error']['Code'] == "ConditionalCheckFailedException":
                print(e.response['Error']['Message'])
            else:
                raise
        else:
            return response

    def get_item(self, user_id):
        try:
            response = self.table.get_item(
                Key={'user_id': user_id}
            )
        except ClientError as e:
            print(e.response['Error']['Message'])
        else:
            return response.get('Item')  # Itemが存在しない場合はNoneを返す

    def reset_count(self, user_id):
        try:
            response = self.table.update_item(
                Key={'user_id': user_id},
                UpdateExpression="set api_calls = :val, last_request_time = :time, start_date = :date, total_api_calls = :val, daily_api_calls = :val",
                ExpressionAttributeValues={
                    ':val': 0,
                    ':time': int(time.time()),
                    ':date': datetime.datetime.now().strftime('%Y-%m-%d')
                },
                ReturnValues="UPDATED_NEW"
            )
        except ClientError as e:
            if e.response['Error']['Code'] == "ConditionalCheckFailedException":
                print(e.response['Error']['Message'])
            else:
                raise
        else:
            return response

    def reset_daily_count(self, user_id):
        try:
            response = self.table.update_item(
                Key={'user_id': user_id},
                UpdateExpression="set api_calls = :val, last_request_time = :time, daily_api_calls = :val",
                ExpressionAttributeValues={
                    ':val': 0,
                    ':time': int(time.time())
                },
                ReturnValues="UPDATED_NEW"
            )
        except ClientError as e:
            if e.response['Error']['Code'] == "ConditionalCheckFailedException":
                print(e.response['Error']['Message'])
            else:
                raise
        else:
            return response


        

class LaunchRequestHandler(AbstractRequestHandler):
    """Handler for Skill Launch."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool

        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "どうしたのー？なんでも聞いてっ！"

        return (
            handler_input.response_builder
                .speak(speak_output)
                .ask(speak_output)
                .response
        )


class ChatGPTIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ChatGPTIntent")(handler_input)

    def handle(self, handler_input):
        slots = handler_input.request_envelope.request.intent.slots
        
        # DynamoDBRateLimiterインスタンスを作成
        question = slots["question"].value
        user_id = handler_input.request_envelope.context.system.user.user_id

        rate_limiter = user_rate_limiters.get(user_id)
        if rate_limiter is None:
            rate_limiter = DynamoDBRateLimiter("RateLimiter", "QuestionRecord")
            user_rate_limiters[user_id] = rate_limiter

        # Try to record the question, but don't stop the program if it fails
        try:
            rate_limiter.record_question(user_id, question)
        except Exception as e:
            print(f"Failed to record question for user {user_id}: {str(e)}")

        # レート制限のチェック
        if not rate_limiter.handle(handler_input):
            # 制限を超えている場合はエラーメッセージを返す
            return (
                handler_input.response_builder
                .speak("ごめんね。　少し休憩しましょう。　しばらくしてからまた話しかけてねー")
                .response
            )

        conversation_history.append(f"ユーザー: {question}")

        prompt = "\n"
        for message in conversation_history:
            prompt += f"{message}\n"

        prompt += "AI: "        

        messages = [
            {"role" : "system", "content":"あなたは非常に優秀な幼稚園の先生です。優しい口調で幼稚園児が相手だと思って会話してください。会話内容はできるだけ５０文字以内で分かりやすくしてください。"},
            {"role" : "user", "content": prompt}
        ]

        # OpenAIのAPIを呼び出すコード
        try:
            res=openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages,
                max_tokens = 200
            )

            speak_output = res['choices'][0]['message']['content']

            speech_text = speak_output

            answer = res['choices'][0]['message']['content']
            conversation_history.append(f"AI: {answer}")
            if res["usage"]["total_tokens"] > 1500:
                conversation_history.pop(0)
                conversation_history.pop(0)

            return (
                handler_input.response_builder
                .speak(speech_text)
                .ask("他に聞きたいことはある？")
                .response
            )

        except Exception as e:
            # API呼び出しエラーが発生した場合の処理
            print(f"API call error occurred for user {user_id}: {str(e)}")
            return (
                handler_input.response_builder
                .speak("現在サービスが混雑しております。少し時間を置いてから再度お試しください。")
                .response
            )



class HelloWorldIntentHandler(AbstractRequestHandler):
    """Handler for Hello World Intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("HelloWorldIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "ハローインテントだよー"

        return (
            handler_input.response_builder
                .speak(speak_output)
                # .ask("add a reprompt if you want to keep the session open for the user to respond")
                .response
        )


class HelpIntentHandler(AbstractRequestHandler):
    """Handler for Help Intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "ヘルプインテントだよー"

        return (
            handler_input.response_builder
                .speak(speak_output)
                .ask(speak_output)
                .response
        )


class CancelOrStopIntentHandler(AbstractRequestHandler):
    """Single handler for Cancel and Stop Intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input) or
                ask_utils.is_intent_name("AMAZON.StopIntent")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "バイバイ、またねー"

        return (
            handler_input.response_builder
                .speak(speak_output)
                .response
        )

class FallbackIntentHandler(AbstractRequestHandler):
    """Single handler for Fallback Intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In FallbackIntentHandler")
        speech = "もういちど。　ゆーっくり言ってみてー"
        reprompt = "わからなかったよー。　もう一回、話してみて"

        return handler_input.response_builder.speak(speech).ask(reprompt).response

class SessionEndedRequestHandler(AbstractRequestHandler):
    """Handler for Session End."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response

        # Any cleanup logic goes here.

        return handler_input.response_builder.response


class IntentReflectorHandler(AbstractRequestHandler):
    """The intent reflector is used for interaction model testing and debugging.
    It will simply repeat the intent the user said. You can create custom handlers
    for your intents by defining them above, then also adding them to the request
    handler chain below.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_request_type("IntentRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        intent_name = ask_utils.get_intent_name(handler_input)
        speak_output = "You just triggered " + intent_name + "."

        return (
            handler_input.response_builder
                .speak(speak_output)
                # .ask("add a reprompt if you want to keep the session open for the user to respond")
                .response
        )


class CatchAllExceptionHandler(AbstractExceptionHandler):
    """Generic error handling to capture any syntax or routing errors. If you receive an error
    stating the request handler chain is not found, you have not implemented a handler for
    the intent being invoked or included it in the skill builder below.
    """
    def can_handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> bool
        return True

    def handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> Response
        logger.error(exception, exc_info=True)

        speak_output = "ごめんね。　ちょっと疲れちゃったみたい。　また後でおはなししようねー"

        return (
            handler_input.response_builder
                .speak(speak_output)
                .ask(speak_output)
                .response
        )



# The SkillBuilder object acts as the entry point for your skill, routing all request and response
# payloads to the handlers above. Make sure any new handlers or interceptors you've
# defined are included below. The order matters - they're processed top to bottom.


sb = SkillBuilder()

sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(ChatGPTIntentHandler())
sb.add_request_handler(HelloWorldIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_request_handler(IntentReflectorHandler()) # make sure IntentReflectorHandler is last so it doesn't override your custom intent handlers

sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
