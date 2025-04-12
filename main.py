from fileinput import filename
import requests
from bs4 import BeautifulSoup
import re
from string import digits
import json
from ModelRecipe import ModelRecipe
import os
import base64
from tqdm import tqdm
import concurrent.futures
import threading
import time
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='scraper.log'
)

# Constants
DEBUG = False
FOLDER_RECIPES = "recipes"
URLS_FILENAME = "URLS"
URLS_FILEPATH = os.path.join(FOLDER_RECIPES, URLS_FILENAME)
MAX_WORKERS = 10
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

# Thread safety
url_file_lock = threading.Lock()
recipe_file_lock = threading.Lock()


def ensure_directories_exist():
    """Ensure necessary directories exist."""
    if not os.path.exists(FOLDER_RECIPES):
        os.makedirs(FOLDER_RECIPES)
    if not os.path.exists(URLS_FILEPATH):
        with open(URLS_FILEPATH, "w") as file:
            pass


def load_urls_file():
    """Load already processed URLs from file."""
    try:
        with open(URLS_FILEPATH, "r") as file:
            return set(file.read().splitlines())
    except Exception as e:
        logging.error(f"Error loading URLs file: {e}")
        return set()


def download_page(url, retries=MAX_RETRIES):
    """Download a page with retries."""
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as e:
            logging.warning(f"Attempt {attempt+1}/{retries} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY)
    logging.error(f"Failed to download {url} after {retries} attempts")
    return None


def find_title(soup):
    """Extract recipe title from soup."""
    if soup is None:
        return None
    tag = soup.find(attrs={"class": "gz-title-recipe gz-mBottom2x"})
    return tag.text if tag else None


def find_calories(soup):
    """Extract calorie information from soup."""
    if soup is None:
        return None
    tag = soup.find(attrs={"class": "gz-text-calories-total"})
    return tag.text.strip() if tag else None


def find_props(soup):
    """Extract recipe properties from soup."""
    if soup is None:
        return None

    properties = {
        "difficulty": "",
        "preparationTime": "",
        "cookingTime": "",
        "servings": "",
        "cost": "",
        "notes": "",
    }

    for tag in soup.find_all(attrs={"class": "gz-name-featured-data"}):
        prop_name = tag.contents[0]
        prop_value = tag.strong.string if tag.strong else ""

        if prop_name.text == "Nota":
            prop_name = "Note"
            prop_value = tag.contents[1]

        if prop_name.startswith("Difficolt"):
            properties["difficulty"] = prop_value
        elif prop_name.startswith("Preparazione"):
            properties["preparationTime"] = prop_value
        elif prop_name.startswith("Cottura"):
            properties["cookingTime"] = prop_value
        elif prop_name.startswith("Dosi per"):
            properties["servings"] = prop_value
        elif prop_name.startswith("Costo"):
            properties["cost"] = prop_value
        elif prop_name.startswith("Note"):
            properties["notes"] = prop_value

    return properties


def find_other(soup):
    """Extract other recipe attributes from soup."""
    if soup is None:
        return None

    other = {
        "vegetarian": False,
        "lactoseFree": False,
    }

    for tag in soup.find_all(attrs={"class": "gz-name-featured-data-other"}):
        if tag.string == "Vegetariano":
            other["vegetarian"] = True
        elif tag.string == "Senza lattosio":
            other["lactoseFree"] = True

    return other


def find_nutritional_info(soup):
    """Extract nutritional information from soup."""
    if soup is None:
        return None

    nutritional_info = {}
    nutritionals_tag = soup.find(attrs={"class": "gz-list-macros"})

    if nutritionals_tag is not None:
        for item in nutritionals_tag.find_all("li"):
            name = item.find("span", attrs={"class": "gz-list-macros-name"}).string
            unit = item.find("span", attrs={"class": "gz-list-macros-unit"}).string
            value = item.find("span", attrs={"class": "gz-list-macros-value"}).string
            nutritional_info[name] = {"unit": unit, "value": value}

    return nutritional_info


def find_ingredients(soup):
    """Extract ingredients from soup."""
    if soup is None:
        return None

    all_ingredients = []

    for tag in soup.find_all(attrs={"class": "gz-ingredient"}):
        name_ingredient = tag.a.string if tag.a else ""
        contents = tag.span.contents[0] if tag.span and tag.span.contents else ""
        quantity_product = re.sub(r"\s+", " ", contents).strip()

        ingredient = {
            "name": name_ingredient,
            "quantity": quantity_product,
        }
        all_ingredients.append(ingredient)

    return all_ingredients


def find_description(soup):
    """Extract recipe description from soup."""
    if soup is None:
        return None

    all_description = ""
    remove_numbers = str.maketrans("", "", digits)

    for tag in soup.find_all(attrs={"class": "gz-content-recipe-step"}):
        if tag.p and hasattr(tag.p, "text"):
            description = tag.p.text.translate(remove_numbers)
            all_description = all_description + description.replace("\xa0", " ")

    return all_description


def find_category(soup):
    """Extract recipe category from soup."""
    if soup is None:
        return None

    for tag in soup.find_all(attrs={"class": "gz-breadcrumb"}):
        if tag.li and tag.li.a:
            return tag.li.a.string
    return None


def get_json_ld(soup):
    """Extract JSON-LD data from soup."""
    if soup is None:
        return None

    try:
        json_ld_tag = soup.find("script", {"type": "application/ld+json"})
        if json_ld_tag:
            return json.loads("".join(json_ld_tag.contents))
    except Exception as e:
        logging.error(f"Error parsing JSON-LD: {e}")
    return None


def find_image(soup):
    """Extract and convert image to base64 from soup."""
    if soup is None:
        return None

    try:
        # Find the first picture tag
        pictures = soup.find("picture", attrs={"class": "gz-featured-image"})

        # Fallback: find a div with class `gz-featured-image-video gz-type-photo`
        if pictures is None:
            pictures = soup.find(
                "div", attrs={"class": "gz-featured-image-video gz-type-photo"}
            )

        if pictures is None:
            return None

        image_source = pictures.find("img")
        if image_source is None:
            return None

        # Most of the times the url is in the `data-src` attribute
        image_url = image_source.get("data-src")

        # Fallback: if not found in `data-src` look for the `src` attr
        if image_url is None:
            image_url = image_source.get("src")

        if image_url:
            image_response = requests.get(image_url, timeout=10)
            image_response.raise_for_status()
            image_to_base64 = str(base64.b64encode(image_response.content))
            return image_to_base64[2:len(image_to_base64) - 1]
    except Exception as e:
        logging.error(f"Error retrieving image: {e}")

    return None


def calculate_file_path(title):
    """Calculate file path for a recipe based on its title."""
    if not title:
        return None
    compact_name = title.replace(" ", "_").lower()
    return os.path.join(FOLDER_RECIPES, f"{compact_name}.json")


def create_file_json(data, path):
    """Create a JSON file with given data at the specified path."""
    try:
        with recipe_file_lock:
            with open(path, "w") as file:
                file.write(json.dumps(data, ensure_ascii=False))
        return True
    except Exception as e:
        logging.error(f"Error creating JSON file at {path}: {e}")
        return False


def append_url_to_file(url):
    """Append a URL to the processed URLs file."""
    try:
        with url_file_lock:
            with open(URLS_FILEPATH, "a") as file:
                file.write(f"{url}\n")
        return True
    except Exception as e:
        logging.error(f"Error appending URL to file: {e}")
        return False


def process_recipe(link, processed_urls):
    """Process a single recipe page."""
    if link in processed_urls:
        return False

    soup = download_page(link)
    if not soup:
        return False

    title = find_title(soup)
    if not title:
        logging.warning(f"Could not find title for {link}")
        return False

    file_path = calculate_file_path(title)
    if os.path.exists(file_path):
        # Already processed
        append_url_to_file(link)
        return True

    # Extract all recipe data
    model_recipe = ModelRecipe()
    model_recipe.title = title
    model_recipe.link = link
    model_recipe.ingredients = find_ingredients(soup)
    model_recipe.description = find_description(soup)
    model_recipe.category = find_category(soup)
    model_recipe.imageBase64 = find_image(soup)

    props = find_props(soup)
    if props:
        model_recipe.difficulty = props["difficulty"]
        model_recipe.preparationTime = props["preparationTime"]
        model_recipe.cookingTime = props["cookingTime"]
        model_recipe.servings = props["servings"]
        model_recipe.price = props["cost"]
        model_recipe.notes = props["notes"]

    model_recipe.nutritionals = find_nutritional_info(soup)
    model_recipe.calories = find_calories(soup)

    other = find_other(soup)
    if other:
        model_recipe.vegetarian = other["vegetarian"]
        model_recipe.lactoseFree = other["lactoseFree"]

    model_recipe.jsonld = get_json_ld(soup)

    # Save recipe and update processed URLs
    success = create_file_json(model_recipe.toDictionary(), file_path)
    if success:
        append_url_to_file(link)
        return True

    return False


def find_recipe_links_on_page(page_number):
    """Find all recipe links on a category page."""
    link_list = f"https://www.giallozafferano.it/ricette-cat/page{page_number}"
    soup = download_page(link_list)

    if not soup:
        return []

    links = []
    for tag in soup.find_all(attrs={"class": "gz-title"}):
        if tag.a and tag.a.get("href"):
            links.append(tag.a.get("href"))

    return links


def process_category_page(page_number, processed_urls):
    """Process a single category page."""
    recipe_links = find_recipe_links_on_page(page_number)
    processed_count = 0

    for link in recipe_links:
        if link not in processed_urls:
            if process_recipe(link, processed_urls):
                processed_count += 1

    return processed_count


def count_total_pages():
    """Count the total number of pages in the category listing."""
    link_list = "https://www.giallozafferano.it/ricette-cat"
    soup = download_page(link_list)

    if not soup:
        return 0

    for tag in soup.find_all(attrs={"class": "disabled total-pages"}):
        try:
            return int(tag.text)
        except (ValueError, TypeError):
            pass

    return 0


def download_all_recipes():
    """Download all recipes with parallel processing."""
    # Ensure the necessary directories exist
    ensure_directories_exist()

    # Load already processed URLs
    processed_urls = load_urls_file()
    logging.info(f"Found {len(processed_urls)} already processed URLs")

    # Count total pages
    total_pages = count_total_pages()
    if total_pages == 0:
        logging.error("Could not determine total pages")
        return

    logging.info(f"Found {total_pages} total pages to process")

    # Process pages in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Create futures for all pages
        futures = {
            executor.submit(process_category_page, page_number, processed_urls): page_number
            for page_number in range(1, total_pages + 1)
        }

        # Process results as they complete
        with tqdm(total=total_pages, desc="Processing pages", ncols=75) as progress:
            for future in concurrent.futures.as_completed(futures):
                page_number = futures[future]
                try:
                    processed_count = future.result()
                    logging.info(f"Page {page_number}: processed {processed_count} recipes")
                except Exception as e:
                    logging.error(f"Error processing page {page_number}: {e}")

                progress.update(1)


if __name__ == "__main__":
    download_all_recipes()
