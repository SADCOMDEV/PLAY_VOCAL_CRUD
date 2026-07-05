from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring

TRANSCRIPT_FILE = "transcripts.log"

DEFAULT_USER_NAME = "Friend"
DEFAULT_CITY_NAME = "UNKNOWN"
DEFAULT_CITY_STATE = "YTD"

def log_transcript(line: str):
    ts = datetime.utcnow().isoformat()
    with open(TRANSCRIPT_FILE, "a") as f:
        f.write(f"{ts} | {line}\n")

class VoiceResponseElement:
    def __init__(self):
        self.response = Element("Response")

    def say(self, text: str):
        say = SubElement(self.response, "Say")
        say.text = text

    def gather_speech(self, action: str, prompt: str, timeout=5):
        gather = SubElement(self.response, "Gather", {
            "input": "speech",
            "action": action,
            "method": "POST",
            "timeout": str(timeout)
        })
        say = SubElement(gather, "Say")
        say.text = prompt

    def gather_dtmf(self, action: str, prompt: str, timeout=5):
        gather = SubElement(self.response, "Gather", {
            "input": "dtmf",
            "numDigits": "1",
            "action": action,
            "method": "POST",
            "timeout": str(timeout)
        })
        say = SubElement(gather, "Say")
        say.text = prompt

    def to_xml(self):
        return tostring(self.response)
