import json
import logging
import os
import urllib.parse
from typing import Any, Dict, List, Optional

import click
import requests
from bs4 import BeautifulSoup
from langchain.chat_models import ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage

def get_logger(log_level: str) -> logging.Logger:
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


class Ask:

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.read_env_variables()

        if logger is not None:
            self.logger = logger
        else:
            self.logger = get_logger("INFO")

        from vectordb import Memory

        self.memory = Memory()

    def read_env_variables(self) -> None:
        err_msg = ""

        self.search_api_key = os.environ.get("SEARCH_API_KEY")
        if self.search_api_key is None:
            err_msg += "SEARCH_API_KEY env variable not set.\n"
        self.search_project_id = os.environ.get("SEARCH_PROJECT_KEY")
        if self.search_project_id is None:
            err_msg += "SEARCH_PROJECT_KEY env variable not set.\n"
        self.llm_api_key = os.environ.get("LLM_API_KEY")
        if self.llm_api_key is None:
            err_msg += "LLM_API_KEY env variable not set.\n"

        if err_msg:
            raise Exception(f"\n{err_msg}\n")

        self.llm_base_url = os.environ.get("LLM_BASE_URL")
        if self.llm_base_url is None:
            self.llm_base_url = "https://api.openai.com/v1"

    def search_web(self, query: str, date_restrict: int, target_site: str) -> List[str]:
        escaped_query = urllib.parse.quote(query)
        url_base = (
            f"https://www.googleapis.com/customsearch/v1?key={self.search_api_key}"
            f"&cx={self.search_project_id}&q={escaped_query}"
        )
        url_paras = "&safe=active"
        if date_restrict is not None and date_restrict > 0:
            url_paras += f"&dateRestrict={date_restrict}"
        if target_site is not None and target_site != "":
            url_paras += f"&siteSearch={target_site}&siteSearchFilter=i"
        url = f"{url_base}{url_paras}"

        self.logger.debug(f"Searching for query: {query}")

        resp = requests.get(url)

        if resp is None:
            raise Exception("No response from search API")

        search_results_dict = json.loads(resp.text)
        if "error" in search_results_dict:
            raise Exception(
                f"Error in search API response: {search_results_dict['error']}"
            )

        if "searchInformation" not in search_results_dict:
            raise Exception(
                f"No search information in search API response: {resp.text}"
            )

        total_results = search_results_dict["searchInformation"].get("totalResults", 0)
        if total_results == 0:
            self.logger.warning(f"No results found for query: {query}")
            return []

        results = search_results_dict.get("items", [])
        if results is None or len(results) == 0:
            self.logger.warning(f"No result items in the response for query: {query}")
            return []

        found_links = []
        for result in results:
            link = result.get("link", None)
            if link is None or link == "":
                self.logger.warning(f"Search result link missing: {result}")
                continue
            found_links.append(link)
        return found_links

    def scrape_urls(self, urls: List[str]) -> Dict[str, str]:
        session = requests.Session()
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0"
        )
        session.headers.update({"User-Agent": user_agent})

        # the key is the url and the value is the body text
        scrape_results: Dict[str, str] = {}

        for url in urls:
            try:
                response = session.get(url, timeout=10)
                soup = BeautifulSoup(response.content, "lxml", from_encoding="utf-8")

                body_tag = soup.body
                if body_tag:
                    body_text = body_tag.get_text()
                    body_text = " ".join(body_text.split()).strip()
                    scrape_results[url] = body_text
                    self.logger.debug(f"Scraped {url}: {body_text}...")
                else:
                    self.logger.warning(
                        f"No body tag found in the response for url: {url}"
                    )
            except Exception as e:
                self.logger.error(f"scraping error {url}: {e}")
                continue
        return scrape_results

    def chunk_results(
        self, scrape_results: Dict[str, str], size: int, overlap: int
    ) -> Dict[str, List[str]]:
        chunking_results: Dict[str, List[str]] = {}
        for url, text in scrape_results.items():
            chunks = []
            for pos in range(0, len(text), size - overlap):
                chunks.append(text[pos : pos + size])
            chunking_results[url] = chunks
        return chunking_results

    def save_to_db(self, chunking_results: Dict[str, List[str]]) -> None:
        for url, chunks in chunking_results.items():
            for i, chunk in enumerate(chunks):
                self.memory.save(texts=chunk, metadata={"url": url, "chunk": i})

    def vector_search(self, query: str) -> List[Dict[str, Any]]:
        results = self.memory.search(query, top_n=10)
        return results

    def _get_chat_model(self, model_name: str) -> ChatOpenAI:
        return ChatOpenAI(
            model_name=model_name,
            openai_api_key=self.llm_api_key,
            openai_api_base=self.llm_base_url,
            temperature=0
        )

    def run_inference(
        self, query: str, model_name: str, matched_chunks: List[Dict[str, Any]]
    ) -> str:
        system_prompt = SystemMessage(
            content="You are expert summarizing the answers based on the provided contents."
        )

        user_prompt_template = """
Given the context as a sequence of references with a reference id in the 
format of a leading [x], please answer the following question:

{query}

In the answer, use format [1], [2], ..., [n] in line where the reference is used. 
For example, "According to the research from Google[3], ...".

Please create the answer strictly related to the context. If the context has no
information about the query, please write "No related information found in the context."

Here is the context:
{context}
"""
        context = ""
        for i, chunk in enumerate(matched_chunks):
            context += f"[{i+1}] {chunk['chunk']}\n"

        human_message = HumanMessage(
            content=user_prompt_template.format(query=query, context=context)
        )

        self.logger.debug(f"Running inference with model: {model_name}")
        self.logger.debug(f"Final user prompt: {human_message.content}")

        chat = self._get_chat_model(model_name)
        response = chat([system_prompt, human_message])

        if response is None:
            raise Exception("No completion from the API")

        return response.content


@click.command(help="Search web for the query and summarize the results")
@click.option("--query", "-q", required=True, help="Query to search")
@click.option(
    "--date-restrict",
    "-d",
    type=int,
    required=False,
    default=None,
    help="Restrict search results to a specific date range, default is no restriction",
)
@click.option(
    "--target-site",
    "-s",
    required=False,
    default=None,
    help="Restrict search results to a specific site, default is no restriction",
)
@click.option(
    "--model-name",
    "-m",
    required=False,
    default="gpt-4o-mini",
    help="Model name to use for inference",
)
@click.option(
    "-l",
    "--log-level",
    "log_level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Set the logging level",
    show_default=True,
)
def search_extract_summarize(
    query: str, date_restrict: int, target_site: str, model_name: str, log_level: str
):
    logger = get_logger(log_level)

    ask = Ask(logger=logger)
    logger.info("✅ Searching the web ...")
    links = ask.search_web(query, date_restrict, target_site)
    logger.info(f"✅ Found {len(links)} links for query: {query}")
    for i, link in enumerate(links):
        logger.debug(f"{i+1}. {link}")

    logger.info("✅ Scraping the URLs ...")
    scrape_results = ask.scrape_urls(links)
    logger.info(f"✅ Scraped {len(scrape_results)} URLs ...")

    logger.info("✅ Chunking the text ...")
    chunking_results = ask.chunk_results(scrape_results, 1000, 100)
    for url, chunks in chunking_results.items():
        logger.debug(f"URL: {url}")
        for i, chunk in enumerate(chunks):
            logger.debug(f"Chunk {i+1}: {chunk}")

    logger.info("✅ Saving to vector DB ...")
    ask.save_to_db(chunking_results)

    logger.info("✅ Querying the vector DB to get context ...")
    results = ask.vector_search(query)
    for i, result in enumerate(results):
        logger.debug(f"{i+1}. {result}")

    logger.info("✅ Running inference with context ...")
    answer = ask.run_inference(query, model_name, results)
    logger.info("✅ Finished inference, generating output ...")
    click.echo("# Answer\n\n{}\n".format(answer))
    click.echo("# References")
    for i, result in enumerate(results):
        click.echo(f"[{i+1}] {result['metadata']['url']}")


if __name__ == "__main__":
    search_extract_summarize()
