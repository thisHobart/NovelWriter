# lore.py
import tkinter as tk
from tkinter import ttk, messagebox
from core.gui.notifications import show_success, show_error, show_warning
from core.gui.task_runner import run_in_background, snapshot_ui
from core.generation.ai_helper import send_prompt, get_backend
from core.generation.prompt_context import format_faction_summary
import json
import os
import logging
from core.generation.helper_fns import open_file, write_file, validate_json_schema, read_json, write_json, validate_json, save_prompt_to_file
from Generators.GenreHandlers import get_genre_handler
from core.localization import zh_field, zh_label
# Character generation now handled through genre handlers
import random
from datetime import datetime

class Lore:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        
        # Initialize directory manager for structured file paths
        from core.config.directory_config import get_directory_manager
        self.dir_manager = get_directory_manager(
            output_dir=app.get_output_dir() if hasattr(app, 'get_output_dir') else 'current_work',
            use_new_structure=True
        )

        # Create main frame
        self.main_frame = ttk.Frame(parent)
        self.main_frame.pack(expand=True, fill='both', padx=10, pady=5)
        
        # Add genre/subgenre display at top
        self.genre_label = ttk.Label(
            self.main_frame, 
            text="当前类型：未选择",
            font=("Arial", 12, "bold")
        )
        self.genre_label.pack(pady=10)
        
        # Update the label initially
        self.update_genre_display()

        # Frame setup for relationships UI
        self.lore_frame = ttk.Frame(parent)
        self.lore_frame.pack(expand=True, fill="both")

        # Title Label
        self.title_label = ttk.Label(self.lore_frame, text="世界观设定", font=("Helvetica", 16))
        self.title_label.pack(pady=10)

        # Add parameter input frame
        self.param_frame = ttk.LabelFrame(self.lore_frame, text="故事参数")
        self.param_frame.pack(pady=10, padx=10, fill="x")

        # Number of Factions input
        self.faction_frame = ttk.Frame(self.param_frame)
        self.faction_frame.pack(fill="x", padx=5, pady=5)
        self.faction_label = ttk.Label(self.faction_frame, text="势力数量（最多 10 个）：")
        self.faction_label.pack(side="left", padx=5)
        self.num_factions_var = tk.StringVar(value="3")
        self.num_factions_entry = ttk.Entry(self.faction_frame, textvariable=self.num_factions_var, width=5)
        self.num_factions_entry.pack(side="left")

        # Number of Characters input
        self.char_frame = ttk.Frame(self.param_frame)
        self.char_frame.pack(fill="x", padx=5, pady=5)
        ttk.Label(self.char_frame, text="人物数量（最多 10 个）：").pack(side="left", padx=5)
        self.num_chars_var = tk.StringVar(value="5")
        self.num_chars_entry = ttk.Entry(self.char_frame, textvariable=self.num_chars_var, width=5)
        self.num_chars_entry.pack(side="left")

        # Additional parameter frame
        self.extra_param_frame = ttk.Frame(self.param_frame)
        self.extra_param_label = ttk.Label(self.extra_param_frame, text="")
        self.extra_param_label.pack(side="left", padx=5)
        self.extra_param_var = tk.StringVar()
        self.extra_param_entry = ttk.Entry(self.extra_param_frame, textvariable=self.extra_param_var, width=15)
        self.extra_param_entry.pack(side="left")

        # Buttons
        # Generate Factions Button
        self.factions_button = ttk.Button(self.lore_frame, text="生成势力", command=self.generate_factions)
        self.factions_button.pack(pady=20)

        # Generate Characters Button
        self.characters_button = ttk.Button(self.lore_frame, text="生成人物", command=self.generate_characters)
        self.characters_button.pack(pady=20)

        # Generate Lore Button
        self.generate_lore_button = ttk.Button(self.lore_frame, text="生成世界观", command=self.generate_lore)
        self.generate_lore_button.pack(pady=20)

        # Enhance Main Characters Button
        self.main_char_enh_button = ttk.Button(self.lore_frame, text="完善主要人物", command=self.main_character_enhancement)
        self.main_char_enh_button.pack(pady=20)

        # Suggest Titles Button
        self.suggest_titles_button = ttk.Button(self.lore_frame, text="推荐作品标题", command=self.suggest_titles)
        self.suggest_titles_button.pack(pady=20)

        # Call update_extra_parameter after all UI elements are created
        self.update_extra_parameter()

    def update_genre_display(self):
        """Update the genre/subgenre display label"""
        try:
            params_ui = self.app.param_ui
            genre = params_ui.genre_var.get()
            subgenre = params_ui.subgenre_var.get()
            self.genre_label.config(
                text=f"当前类型：{zh_label(genre)} - {zh_label(subgenre)}"
            )
        except AttributeError:
            self.genre_label.config(text="当前类型：未连接参数")

    def update_extra_parameter(self):
        """Update UI based on selected subgenre"""
        self.update_genre_display()
        try:
            # Update faction label based on genre
            genre = self.app.param_ui.genre_var.get()
            try:
                genre_handler = get_genre_handler(genre)
                if genre_handler.uses_factions():
                    organization_type = genre_handler.get_organization_type()
                    if organization_type == "factions":
                        self.faction_label.config(text="势力数量（最多 10 个）：")
                        self.factions_button.config(text="生成势力")
                    elif organization_type == "agencies":
                        self.faction_label.config(text="机构数量（最多 10 个）：")
                        self.factions_button.config(text="生成机构")
                    elif organization_type == "social circles":
                        self.faction_label.config(text="社会团体数量（最多 10 个）：")
                        self.factions_button.config(text="生成社会团体")
                    elif organization_type == "cults":
                        self.faction_label.config(text="教团/组织数量（最多 10 个）：")
                        self.factions_button.config(text="生成教团/组织")
                    else:
                        self.faction_label.config(text=f"组织数量（最多 10 个，类型：{organization_type}）：")
                        self.factions_button.config(text="生成组织")
                else:
                    # Hide faction generation for genres that don't use them
                    self.faction_frame.pack_forget()
                    self.factions_button.pack_forget()
                    return
            except ValueError:
                # Default to factions if genre handler not found
                self.faction_label.config(text="势力数量（最多 10 个）：")
                self.factions_button.config(text="生成势力")
            
            # Ensure faction frame and button are visible for genres that use them
            if not self.faction_frame.winfo_manager():
                self.faction_frame.pack(fill="x", padx=5, pady=5, after=self.genre_label)
            if not self.factions_button.winfo_manager():
                self.factions_button.pack(pady=20, before=self.characters_button)
            
            subgenre = self.app.param_ui.subgenre_var.get()
            
            # Handle other extra parameters
            extra_params = {
                # Sci-Fi subgenres
                "Cyberpunk": "技术重点：",
                "Military Sci-Fi": "冲突规模：",
                "Post-Apocalyptic": "灾难类型：",
                "Hard Science Fiction": "科学重点：",
                "Time Travel": "时间范围：",
                "Alternate History": "历史分歧点：",
                "Dystopian": "社会议题重点：",
                # Fantasy subgenres
                "High Fantasy": "魔法体系重点：",
                "Dark Fantasy": "恐怖元素：",
                "Urban Fantasy": "现代背景：",
                "Sword and Sorcery": "冒险重点：",
                "Mythic Fantasy": "神话重点：",
                "Fairy Tale": "寓意主题：",
            }

            if subgenre in extra_params:
                self.extra_param_label.config(text=extra_params[subgenre])
                self.extra_param_frame.pack(fill="x", padx=5, pady=5)
            else:
                self.extra_param_frame.pack_forget()

        except Exception as e:
            self.app.logger.error(f"Failed to update extra parameter: {e}", exc_info=True)

# Generation functions

    # Generate list of factions and some of their details
    def _busy_widgets(self):
        """后台任务运行期间需要锁住的按钮。"""
        return (
            self.factions_button,
            self.characters_button,
            self.generate_lore_button,
            self.main_char_enh_button,
            self.suggest_titles_button,
        )

    def generate_factions(self):
        """读取界面输入后，把生成工作交给后台线程（见 core/gui/task_runner.py）。"""
        try:
            ui = snapshot_ui(
                self.app,
                num_factions=int(self.num_factions_var.get()),
            )
        except (TypeError, ValueError):
            show_error("错误", "请输入有效的数量。")
            return
        run_in_background(
            self.app.root,
            lambda: self._generate_factions(ui),
            on_error=lambda exc: show_error("错误", str(exc)),
            busy_widgets=self._busy_widgets(),
            busy_button=self.factions_button,
            busy_text="正在生成…",
            logger=self.app.logger if self.app else None,
        )

    def _generate_factions(self, ui):
        try:
            # Get the number of factions from the UI
            num_factions = ui.get("num_factions")
            
            # Get the selected gender bias percentages from ParametersUI
            params = ui.parameters
            female_percentage = params.get("female_percentage", 50) # Default to 50 if not found
            male_percentage = params.get("male_percentage", 50)   # Default to 50 if not found
            genre = params.get("genre", "Sci-Fi")  # Get current genre
            subgenre = params.get("subgenre", "")  # Get current subgenre
            
            self.app.logger.info(f"Generating {genre} factions (using gender bias: Female {female_percentage}%, Male {male_percentage}%)")
            
            # Get the appropriate genre handler
            try:
                genre_handler = get_genre_handler(genre)
            except ValueError as e:
                self.app.logger.error(f"Unsupported genre: {genre}. Error: {e}")
                show_error("错误", f"不支持的类型：{genre}")
                return
            
            # Generate factions using the genre handler
            factions = genre_handler.generate_factions(
                num_factions=num_factions,
                female_percentage=female_percentage,
                male_percentage=male_percentage,
                subgenre=subgenre
            )
            
            # Print factions to console for debugging
            if factions:
                first_name = factions[0].get('faction_name') or factions[0].get('name') or 'N/A'
                self.app.logger.info(f"Generated {len(factions)} factions. First faction example: {first_name}")
                for i, faction_data in enumerate(factions):
                    self.app.logger.debug(f"Faction {i+1} Summary:")
                    faction_name = faction_data.get('faction_name') or faction_data.get('name') or 'N/A'
                    faction_profile = faction_data.get('faction_profile') or faction_data.get('description') or 'N/A'
                    self.app.logger.debug(f"  Name: {faction_name}")
                    self.app.logger.debug(f"  Profile: {faction_profile}")
                    self.app.logger.debug(f"  Systems: {len(faction_data.get('systems', []))}")
            else:
                self.app.logger.warning("Faction generation returned no factions.")

            # Determine the output directory from the app settings
            output_dir = ui.output_dir
            
            # Use structured directory for factions file
            lore_dir = self.dir_manager.get_path('lore_dir')
            lore_full_path = os.path.join(output_dir, lore_dir)
            os.makedirs(lore_full_path, exist_ok=True) # Ensure the lore directory exists
            
            # Construct the full filepath for factions.json in structured directory
            factions_filepath = os.path.join(lore_full_path, "factions.json")
            
            # Save factions to file using the genre handler
            genre_handler.save_factions(factions, factions_filepath)

            self.app.logger.info(f"Generated {genre} factions and saved to {factions_filepath}")
            # show_success("Success", f"Generated {genre} factions and saved successfully.")

        except AttributeError as ae:
            # This might happen if parameters_ui or gender_bias_var is not found
            self.app.logger.error(f"AttributeError in generate_factions: {ae}. UI element access issue?", exc_info=True)
            show_error("错误", f"界面元素访问错误：{str(ae)}")
        except Exception as e:
            self.app.logger.error(f"Failed to generate faction details: {e}", exc_info=True)
            show_error("错误", f"生成势力详情失败：{str(e)}")

    # Generate a list of characters
    # Then match characters to the list of factions
    # Also generates relationships between characters?
    def generate_characters(self):
        """读取界面输入后，把生成工作交给后台线程（见 core/gui/task_runner.py）。"""
        try:
            ui = snapshot_ui(
                self.app,
                num_chars=int(self.num_chars_var.get()),
            )
        except (TypeError, ValueError):
            show_error("错误", "请输入有效的数量。")
            return
        run_in_background(
            self.app.root,
            lambda: self._generate_characters(ui),
            on_error=lambda exc: show_error("错误", str(exc)),
            busy_widgets=self._busy_widgets(),
            busy_button=self.characters_button,
            busy_text="正在生成…",
            logger=self.app.logger if self.app else None,
        )

    def _generate_characters(self, ui):
        try:
            num_chars = ui.get("num_chars")
            self.app.logger.info(f"Attempting to generate {num_chars} characters.")
            
            # Get the selected gender bias percentages and genre from Parameters.py
            params = ui.parameters
            female_percentage = params.get("female_percentage", 50)
            male_percentage = params.get("male_percentage", 50)
            genre = params.get("genre", "Sci-Fi")
            
            self.app.logger.info(f"Using gender bias for character generation: Female {female_percentage}%, Male {male_percentage}%")
            self.app.logger.info(f"Generating characters for genre: {genre}")
            
            # Generate characters using the genre handler system
            try:
                genre_handler = get_genre_handler(genre)
                characters = genre_handler.generate_characters(
                    num_characters=num_chars,
                    female_percentage=female_percentage,
                    male_percentage=male_percentage,
                    include_races=True  # This will be ignored by sci-fi handler, used by fantasy handler
                )
            except ValueError as e:
                self.app.logger.error(f"Unsupported genre for character generation: {genre}. Error: {e}")
                show_error("错误", f"该类型不支持生成人物：{genre}")
                return
            
            if not characters:
                self.app.logger.error("Failed to generate characters. generate_main_characters returned empty.")
                show_error("错误", "生成人物失败。")
                return
            
            # Note: Characters are now generated with genre-appropriate attributes from the start
            # No post-processing needed as each generator handles its own genre-specific attributes
            
            # Print to console for debugging
            # for char in characters:
            #     print_character(char) # Replaced by logger below
            self.app.logger.info(f"Successfully generated {len(characters)} characters.")
            for i, char_data in enumerate(characters):
                # char_data is a Character object, not a dict. Access attributes directly or use getattr.
                char_name = getattr(char_data, 'name', 'N/A')
                char_role = getattr(char_data, 'role', 'N/A')
                self.app.logger.debug(f"Character {i+1}: {char_name} ({char_role})")
            
            # --- Add Gender Count for Main Characters ---
            female_main_char_count = 0
            male_main_char_count = 0
            for char_obj in characters:
                if hasattr(char_obj, 'gender'):
                    if char_obj.gender == "Female":
                        female_main_char_count += 1
                    elif char_obj.gender == "Male":
                        male_main_char_count += 1
            
            total_main_chars = len(characters)
            if total_main_chars > 0:
                female_actual_percentage = (female_main_char_count / total_main_chars) * 100
                male_actual_percentage = (male_main_char_count / total_main_chars) * 100
                self.app.logger.info(f"MAIN CHARACTER GENDER SUMMARY: Total={total_main_chars}, Females={female_main_char_count} ({female_actual_percentage:.2f}%), Males={male_main_char_count} ({male_actual_percentage:.2f}%)")
                self.app.logger.info(f"  (Expected based on input: Female {female_percentage}%, Male {male_percentage}%)")
            else:
                self.app.logger.info("MAIN CHARACTER GENDER SUMMARY: No main characters generated to summarize.")
            # --- End Gender Count ---
            
            # Save to file
            output_dir = ui.output_dir
            
            # Use structured directory for characters file
            lore_dir = self.dir_manager.get_path('lore_dir')
            lore_full_path = os.path.join(output_dir, lore_dir)
            os.makedirs(lore_full_path, exist_ok=True)
            characters_filepath = os.path.join(lore_full_path, "characters.json")
            
            # Save using the genre handler's save function
            genre_handler.save_characters(characters, filename=characters_filepath)
            
            self.app.logger.info(f"Generated main characters and saved to {characters_filepath}")
            # show_success("Success", "Generated characters and saved successfully.")

        except Exception as e:
            self.app.logger.error(f"Failed to generate character details: {e}", exc_info=True)
            show_error("错误", f"生成人物详情失败：{str(e)}")

    def _add_genre_specific_attributes(self, characters, genre_handler):
        """Add genre-specific attributes to characters based on the genre handler."""
        try:
            genre_name = genre_handler.get_genre_name()
            
            # Check if this genre uses factions/organizations
            if not genre_handler.uses_factions():
                self.app.logger.info(f"{genre_name} doesn't use traditional factions - skipping faction assignment")
                return
            
            # Load faction data to assign characters to factions/organizations
            output_dir = self.app.get_output_dir()
            
            # Use structured directory for factions file
            lore_dir = self.dir_manager.get_path('lore_dir')
            lore_full_path = os.path.join(output_dir, lore_dir)
            factions_filepath = os.path.join(lore_full_path, "factions.json")
            
            factions_data = None
            try:
                factions_data = read_json(factions_filepath)
                organization_type = genre_handler.get_organization_type()
                self.app.logger.info(f"Loaded {len(factions_data)} {organization_type} for character assignment")
            except FileNotFoundError:
                self.app.logger.warning(f"No {genre_handler.get_organization_type()} file found - characters will be generated without organizational affiliations")
                return
            except Exception as e:
                self.app.logger.warning(f"Error loading {genre_handler.get_organization_type()} for character assignment: {e}")
                return
            
            if not factions_data:
                return
            
            if genre_name == "Sci-Fi":
                self._assign_scifi_attributes(characters, factions_data)
            elif genre_name == "Fantasy":
                self._assign_fantasy_attributes(characters, factions_data)
            else:
                # For other genres that use factions, assign basic faction affiliation
                self._assign_basic_faction_attributes(characters, factions_data, genre_handler)
                
        except Exception as e:
            self.app.logger.error(f"Error adding genre-specific attributes: {e}", exc_info=True)
    
    def _assign_scifi_attributes(self, characters, factions_data):
        """Assign sci-fi specific attributes like homeworld and home_system."""
        # Get list of all habitable planets
        habitable_planets = []
        for faction in factions_data:
            for system in faction.get("systems", []):
                for planet in system.get("habitable_planets", []):
                    habitable_planets.append({
                        "name": planet.get("name", "Unknown"),
                        "system": system.get("name", "Unknown"),
                        "faction": faction.get("faction_name", "Unknown")
                    })
        
        if not habitable_planets:
            self.app.logger.warning("No habitable planets found in factions data")
            return
            
        # Assign homeworld and faction to each character
        for char in characters:
            homeworld = random.choice(habitable_planets)
            char.homeworld = homeworld["name"]
            char.home_system = homeworld["system"]
            char.faction = homeworld["faction"]
            
    def _assign_fantasy_attributes(self, characters, factions_data):
        """Assign fantasy specific attributes like homeland, home_region, and race."""
        # Get list of all cities and their regions
        cities_and_regions = []
        for faction in factions_data:
            faction_race = faction.get("race", "Human")  # Get faction's race
            for region in faction.get("regions", []):
                for city in region.get("cities", []):
                    cities_and_regions.append({
                        "name": city.get("name", "Unknown"),
                        "region": region.get("name", "Unknown"),
                        "faction": faction.get("faction_name", "Unknown"),
                        "race": faction_race
                    })
        
        if not cities_and_regions:
            self.app.logger.warning("No cities found in factions data")
            return
            
        # Assign homeland, region, race, and faction to each character
        for char in characters:
            homeland_info = random.choice(cities_and_regions)
            char.homeland = homeland_info["name"]
            char.home_region = homeland_info["region"]
            char.race = homeland_info["race"]
            char.faction = homeland_info["faction"]
            
            # Add fantasy-specific attributes if they don't exist
            if not hasattr(char, 'homeland'):
                char.homeland = None
            if not hasattr(char, 'home_region'):
                char.home_region = None
            if not hasattr(char, 'race'):
                char.race = None
    
    def _assign_basic_faction_attributes(self, characters, factions_data, genre_handler):
        """Assign basic faction/organization affiliation for genres that use them."""
        organization_type = genre_handler.get_organization_type()
        
        # Extract organization names from the factions data
        organizations = []
        for faction in factions_data:
            organizations.append({
                "name": faction.get("name", "Unknown Organization"),
                "type": faction.get("type", "Unknown Type")
            })
        
        if not organizations:
            self.app.logger.warning(f"No {organization_type} found in data")
            return
            
        # Assign organization affiliation to each character
        for char in characters:
            org_info = random.choice(organizations)
            char.faction = org_info["name"]
            
            # Add organization type as an attribute for some genres
            if hasattr(char, 'organization_type') or organization_type in ['agencies', 'cults']:
                char.organization_type = org_info["type"]


   ### Generation Functions -- leveraging LLMs for content generation


    def generate_lore(self):
        """读取界面输入后，把生成工作交给后台线程（见 core/gui/task_runner.py）。"""
        ui = snapshot_ui(self.app)
        run_in_background(
            self.app.root,
            lambda: self._generate_lore(ui),
            on_error=lambda exc: show_error("错误", str(exc)),
            busy_widgets=self._busy_widgets(),
            busy_button=self.generate_lore_button,
            busy_text="正在生成世界观…",
            logger=self.app.logger if self.app else None,
        )

    def _generate_lore(self, ui):
        """Generate lore using an internally constructed prompt and LLM"""
        self.app.logger.info("Lore generation process started.")
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Using model: {selected_model} for lore generation.")
        self.app.logger.info(f"Output directory for lore files: {output_dir}")

        try:
            # Define and create the prompts subdirectory
            prompts_subdir = os.path.join(output_dir, "system", "prompts")
            os.makedirs(prompts_subdir, exist_ok=True)
            self.app.logger.info(f"Ensured prompts subdirectory exists at: {prompts_subdir}")

            # --- Load necessary data ---
            parameters_txt_path = os.path.join(output_dir, "system", "parameters.txt")
            characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
            factions_json_path = os.path.join(output_dir, "story", "lore", "factions.json")

            story_params = {}
            try:
                if os.path.exists(parameters_txt_path):
                    params_content = open_file(parameters_txt_path)
                    for line in params_content.splitlines():
                        if ":" in line:
                            key, value = line.split(":", 1)
                            story_params[key.strip()] = value.strip()
                    self.app.logger.info(f"Loaded parameters from {parameters_txt_path}")
                else:
                    self.app.logger.warning(f"Parameters file not found: {parameters_txt_path}. Proceeding without detailed parameters.")
            except Exception as e:
                self.app.logger.error(f"Error loading parameters from {parameters_txt_path}: {e}", exc_info=True)


            characters = []
            try:
                all_character_data = read_json(characters_json_path)
                characters = all_character_data.get("characters", [])
                if not characters:
                    self.app.logger.warning(f"No characters found in {characters_json_path}")
                else:
                    self.app.logger.info(f"Loaded {len(characters)} characters from {characters_json_path}")
            except FileNotFoundError:
                self.app.logger.warning(f"Character file not found: {characters_json_path}")
            except (json.JSONDecodeError, ValueError) as e:
                self.app.logger.error(f"Error loading character data from {characters_json_path}: {e}", exc_info=True)

            factions = []
            try:
                factions_data = read_json(factions_json_path)
                if factions_data:
                    # Handle both direct list format and wrapped format
                    if isinstance(factions_data, list):
                        # Direct list format (used by some genres)
                        factions = factions_data
                    elif isinstance(factions_data, dict) and "factions" in factions_data:
                        # Wrapped format (used by Horror and potentially other genres)
                        factions = factions_data["factions"]
                    else:
                        # Fallback: try to use the data as-is if it's a dict with faction-like structure
                        factions = factions_data if isinstance(factions_data, list) else []
                    
                    self.app.logger.info(f"Loaded {len(factions)} factions from {factions_json_path}")
                else:
                    self.app.logger.warning(f"No factions found or empty data in {factions_json_path}")
            except FileNotFoundError:
                self.app.logger.warning(f"Faction file not found: {factions_json_path}")
            except (json.JSONDecodeError, ValueError) as e:
                self.app.logger.error(f"Error loading faction data from {factions_json_path}: {e}", exc_info=True)

            # --- Step 1: Construct the base prompt ---
            self.app.logger.info("Constructing base lore prompt...")
            prompt_lines = [
                "请为一部新故事创建基础世界观设定。",
                "根据提供的参数、人物摘要和势力摘要，生成丰富而详细的故事世界背景。",
                # "- Key historical events.",
                # "- Cultural details.",
                # "- Technological level and unique aspects.",
                # "- Potential conflicts and mysteries.",
                # "- Initial plot points or hooks.",
                "请确保世界观与所有已提供的信息一致。"
            ]

            prompt_lines.append("\n## 故事参数：")
            if story_params:
                for key, value in story_params.items():
                    prompt_lines.append(f"- {key}: {value}")
            else:
                prompt_lines.append("- 未加载参数")

            prompt_lines.append("\n## 人物摘要：")
            if characters:
                for char_dict in characters:
                    name = char_dict.get('name', '未知人物')
                    role = char_dict.get('role', '未知角色')
                    prompt_lines.append(f"- {name} ({role})")
            else:
                prompt_lines.append("- 未加载人物")

            prompt_lines.append("\n## 势力摘要：")
            prompt_lines.append("\n请重点处理前两个势力，其他势力稍后再处理。\n")
            if factions:
                prompt_lines.append(format_faction_summary(factions))
            else:
                prompt_lines.append("- 未加载势力")
            
            # Initial prompt content is now built
            prompt = "\n".join(prompt_lines)
            
            # --- Step 2: Enhance the prompt with detailed character and faction information ---
            self.app.logger.info("Enhancing prompt with faction capitals and detailed character info...")
            if factions:
                # Get current genre and appropriate handler
                params = ui.parameters
                current_genre = params.get("genre", "Sci-Fi")
                
                try:
                    genre_handler = get_genre_handler(current_genre)
                    faction_section = genre_handler.get_faction_capitals_info(factions)
                    prompt += faction_section
                except ValueError as e:
                    self.app.logger.warning(f"Could not get genre handler for {current_genre}: {e}. Skipping faction capitals.")
                    # Fallback: add basic faction names only
                    faction_section = "\n## 势力名称：\n"
                    for faction in factions:
                        faction_name = faction.get("faction_name") or faction.get("name") or "未知势力"
                        faction_section += f"- {faction_name}\n"
                    prompt += faction_section

            if characters:
                # Add a detailed character section to the prompt
                character_section = "\n## 人物详细信息：\n"
                
                # Sort characters by role priority
                role_priority = {"protagonist": 0, "deuteragonist": 1, "antagonist": 2}
                # Ensure characters is a list of dicts here when loaded from JSON
                sorted_chars = sorted(characters, key=lambda x: role_priority.get(x.get("role", "").lower(), 99))
                
                for char_dict in sorted_chars: # char_dict is a dictionary from characters.json
                    char_name = char_dict.get('name', 'Unknown') # Use .get() for dict
                    char_role = char_dict.get('role', '未知角色') # Use .get() for dict
                    char_section_detail = f"\n### {char_name} ({char_role}):\n"
                    
                    # Add basic information
                    basic_info = []
                    # Get character attributes from genre handler
                    try:
                        genre_handler = get_genre_handler(current_genre)
                        basic_keys = genre_handler.get_character_attributes()
                    except ValueError:
                        # Fallback to basic attributes if genre handler not found
                        basic_keys = ['gender', 'age', 'title', 'occupation', 'faction', 'faction_role', 
                                    'goals', 'motivations', 'flaws', 'strengths', 'arc']
                    
                    for key in basic_keys:
                        value = char_dict.get(key)
                        if value:
                            if isinstance(value, list):
                                basic_info.append(f"- {zh_field(key)}：{', '.join(value)}")
                            else:
                                basic_info.append(f"- {zh_field(key)}：{value}")
                    
                    # Add character traits (this section is now redundant since basic_keys already includes these)
                    traits = []
                    
                    family_data = char_dict.get('family', {})
                    if family_data:
                        family_info_list = ["- 家庭："]
                        parents = family_data.get('parents', [])
                        if parents:
                            parents_str = ", ".join([f"{p.get('name', '无')} ({p.get('relation', '无')}, {p.get('gender', '无')}, {p.get('status', '无')})"
                                                   for p in parents])
                            family_info_list.append(f"  - 父母：{parents_str}")
                        siblings = family_data.get('siblings', [])
                        if siblings:
                            siblings_str = ", ".join([f"{s.get('name', '无')} ({s.get('relation', '无')}, {s.get('gender', '无')})"
                                                    for s in siblings])
                            family_info_list.append(f"  - 兄弟姐妹：{siblings_str}")
                        spouse = family_data.get('spouse')
                        if spouse and isinstance(spouse, dict):
                            family_info_list.append(f"  - 配偶：{spouse.get('name', '无')}（{spouse.get('gender', '无')}）")
                        children_val = family_data.get('children', [])
                        if children_val:
                            children_str = ", ".join([f"{c.get('name', '无')} ({c.get('relation', '无')}, {c.get('gender', '无')})"
                                                    for c in children_val])
                            family_info_list.append(f"  - 子女：{children_str}")
                        # Only extend traits if family_info_list has more than just the "- Family:" header
                        if len(family_info_list) > 1:
                             traits.extend(family_info_list)
                    
                    # Combine all information
                    char_section_detail += "\n".join(basic_info + traits)
                    character_section += char_section_detail

                # Add the character section to the prompt
                prompt += character_section
                
            prompt += "\n\n## 最终要求：\n请确保生成的世界观与上述人物细节和势力信息一致，尤其注意性别、关系和个人背景。在构建更广阔的世界背景时，应保留并尊重这些属性。"
            prompt += "\n\n重要：只生成所需的世界观、背景和初始情节点，不要在本次响应中生成故事标题；标题将单独生成和管理。"
            prompt += "\n\n现在生成世界观设定："

            # --- Step 3: Save the final assembled prompt to a single, non-timestamped file ---
            main_lore_prompt_filepath = os.path.join(prompts_subdir, "main_lore_prompt.md")
            try:
                write_file(main_lore_prompt_filepath, prompt)
                self.app.logger.info(f"Definitive Main Lore Generation Prompt (length {len(prompt)}) saved to: {main_lore_prompt_filepath}")
            except IOError as e_write:
                self.app.logger.error(f"Failed to write Main Lore Generation Prompt to {main_lore_prompt_filepath}: {e_write}", exc_info=True)
                show_error("错误", f"保存世界观 Prompt 失败：{main_lore_prompt_filepath}")
                return False # Stop if we can't save the prompt

            # --- Step 4: Send the enhanced prompt to the LLM ---
            current_backend = get_backend()
            backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
            self.app.logger.info(f"Sending main lore prompt from {main_lore_prompt_filepath} to LLM ({backend_info})...")
            lore_text = send_prompt(prompt, model=selected_model)
            
            if not lore_text:
                self.app.logger.error(f"Failed to generate lore from LLM ({backend_info}). Received no response.")
                show_error("错误", "世界观生成失败（大模型未返回内容）。")
                return False
            
            self.app.logger.info(f"Lore successfully generated by LLM. Response length: {len(lore_text)} chars.")
            # --- Step 5: Save the generated lore ---
            self.app.logger.info("Saving generated lore...")
            
            # Use structured directory for generated lore file
            lore_dir = self.dir_manager.get_path('lore_dir')
            lore_full_path = os.path.join(output_dir, lore_dir)
            os.makedirs(lore_full_path, exist_ok=True)
            generated_lore_filepath = os.path.join(lore_full_path, "generated_lore.md")
            write_file(generated_lore_filepath, lore_text)
            
            self.app.logger.info(f"Lore successfully generated and saved to {generated_lore_filepath}")
            # show_success("Success", f"Lore generated and saved to {generated_lore_filepath}.\n\nPrompt used is in {main_lore_prompt_filepath}")
            return True

        except Exception as e:
            self.app.logger.error(f"Error during lore generation process: {e}", exc_info=True)
            show_error("错误", f"生成世界观时出错：{e}")
            return False


    # New function to suggest titles
    def suggest_titles(self):
        """读取界面输入后，把生成工作交给后台线程（见 core/gui/task_runner.py）。"""
        ui = snapshot_ui(self.app)
        run_in_background(
            self.app.root,
            lambda: self._suggest_titles(ui),
            on_error=lambda exc: show_error("错误", str(exc)),
            busy_widgets=self._busy_widgets(),
            busy_button=self.suggest_titles_button,
            busy_text="正在推荐…",
            logger=self.app.logger if self.app else None,
        )

    def _suggest_titles(self, ui):
        self.app.logger.info("Title suggestion process started.")
        selected_model = ui.model
        output_dir = ui.output_dir # This is typically "current_work"
        # The save_prompt_to_file function will handle creating the 'prompts' subdirectory within output_dir.
        # os.makedirs(os.path.join(output_dir, "prompts"), exist_ok=True) # Ensured by save_prompt_to_file

        lore_file_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        params_file_path = os.path.join(output_dir, "system", "parameters.txt")

        try:
            # 1. Load Lore Content
            if not os.path.exists(lore_file_path):
                self.app.logger.error(f"Lore file not found at {lore_file_path}. Cannot suggest titles.")
                show_error("错误", f"找不到世界观文件（{lore_file_path}），请先生成世界观。")
                return
            lore_content = open_file(lore_file_path)
            self.app.logger.info(f"Loaded lore content from {lore_file_path} for title suggestion.")

            # 2. Load Parameters (for genre, subgenre, themes - optional but good context)
            story_genre = "fiction"  # Default
            story_subgenre = ""    # Default
            story_themes = ""      # Default to empty, will be updated if themes are found

            try:
                if os.path.exists(params_file_path):
                    params_content = open_file(params_file_path)
                    current_params = {}
                    for line in params_content.splitlines():
                        if ":" in line:
                            key, value = line.split(":", 1)
                            current_params[key.strip().lower().replace(' ','_')] = value.strip()
                    story_genre = current_params.get('genre', story_genre)
                    story_subgenre = current_params.get('subgenre', story_subgenre)
                    # Get theme, if it's "Not specified" or empty, story_themes will reflect that or be empty.
                    story_themes = current_params.get('theme', '').strip()
                    self.app.logger.info(f"Loaded parameters for title context: Genre='{story_genre}', Subgenre='{story_subgenre}', Theme='{story_themes}'.")
                else:
                    self.app.logger.warning(f"Parameters file not found at {params_file_path}. Proceeding without theme/genre context for titles.")
            except Exception as e_params:
                self.app.logger.warning(f"Could not parse parameters from {params_file_path} for title context: {e_params}")

            # 3. Construct the prompt for title suggestions
            # Use full lore content, no truncation
            lore_for_prompt = lore_content

            prompt_lines = [
                f"以下是{zh_label(story_subgenre)}{zh_label(story_genre)}故事的世界观设定。请根据这些设定以及列出的主题（如有），推荐 5—10 个可用标题。",
                "请使用简单的编号列表，每行一个标题。\n\n例如：\n",
                "1. 标题一\n",
                "2. 另一个好标题\n",
                "3. 最后的建议\n",
                "",
                "## 故事世界观：",
                lore_for_prompt, # Using full lore
                ""
            ]
            # Conditionally add the "Key Themes" section
            if story_themes and story_themes.lower() != "not specified":
                prompt_lines.append(f"## 关键主题：{story_themes}")
                prompt_lines.append("")

            prompt_lines.append("响应中只提供标题编号列表：")
            title_prompt_content = "\n".join(prompt_lines)

            # 4. Save the title suggestion prompt
            title_prompt_base_name = "title_suggestion_prompt"
            # Pass output_dir directly; save_prompt_to_file will place it in the 'prompts' subfolder by default.
            title_prompt_filepath = save_prompt_to_file(output_dir, title_prompt_base_name, title_prompt_content)

            if title_prompt_filepath:
                self.app.logger.info(f"Title suggestion prompt (length {len(title_prompt_content)}) saved to: {title_prompt_filepath}")
            else:
                self.app.logger.error(f"Failed to save title suggestion prompt. Length: {len(title_prompt_content)}.")
                # Fallback logging if needed (similar to other prompt saves)
                if self.app.logger.isEnabledFor(logging.DEBUG):
                    self.app.logger.debug(f"Fallback: Full title suggestion prompt:\n{title_prompt_content}")
                else:
                    self.app.logger.warning("Title suggestion prompt content not logged. Enable DEBUG for full prompt.")

            # 5. Send to LLM
            log_msg_prompt_source = f"(from {title_prompt_filepath})" if title_prompt_filepath else "(from memory, save failed)"
            current_backend = get_backend()
            backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
            self.app.logger.info(f"Sending title suggestion prompt {log_msg_prompt_source} to LLM (backend: {backend_info})...")
            suggested_titles_text = send_prompt(title_prompt_content, model=selected_model)

            if not suggested_titles_text:
                self.app.logger.error("Failed to get title suggestions from LLM.")
                show_error("错误", "无法从大模型获取标题建议。")
                return

            self.app.logger.info(f"Received title suggestions from LLM. Length: {len(suggested_titles_text)}.")

            # 6. Save suggested titles to a file
            # Use structured directory for suggested titles file
            planning_dir = self.dir_manager.get_path('planning_dir')
            planning_full_path = os.path.join(output_dir, planning_dir)
            os.makedirs(planning_full_path, exist_ok=True)
            suggested_titles_filepath = os.path.join(planning_full_path, "suggested_titles.md")
            try:
                write_file(suggested_titles_filepath, suggested_titles_text)
                self.app.logger.info(f"Suggested titles saved to: {suggested_titles_filepath}")
                # show_success("Success", f"Title suggestions have been saved to:\n{suggested_titles_filepath}\n\nPlease review this file and then update the Novel Title in the Parameters tab.")
            except IOError as e_write:
                self.app.logger.error(f"Failed to write suggested titles to {suggested_titles_filepath}: {e_write}", exc_info=True)
                # show_error("Error", f"Failed to save suggested titles to file: {e_write}")

        except FileNotFoundError as fnf_e:
            self.app.logger.error(f"File not found during title suggestion: {fnf_e}", exc_info=True)
            # Messagebox likely shown by specific file check earlier.
        except Exception as e:
            self.app.logger.error(f"An error occurred during title suggestion: {e}", exc_info=True)
            show_error("错误", f"推荐标题时发生意外错误：{str(e)}")


    # Generate background story for the main characters
    def main_character_enhancement(self):
        """读取界面输入后，把生成工作交给后台线程（见 core/gui/task_runner.py）。"""
        ui = snapshot_ui(self.app)
        run_in_background(
            self.app.root,
            lambda: self._main_character_enhancement(ui),
            on_error=lambda exc: show_error("错误", str(exc)),
            busy_widgets=self._busy_widgets(),
            busy_button=self.main_char_enh_button,
            busy_text="正在完善…",
            logger=self.app.logger if self.app else None,
        )

    def _main_character_enhancement(self, ui):
        self.app.logger.info("Main character enhancement process started.")
        selected_model = ui.model 
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)

        self.app.logger.info(f"Using model: {selected_model} for main character enhancement")
        self.app.logger.info(f"Output directory for character files: {output_dir}")

        # Construct full paths for input files using structured directories
        characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
        generated_lore_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")

        try:
            # Load generated lore
            try:
                lore_content = open_file(generated_lore_path)
                self.app.logger.info(f"Loaded lore content from {generated_lore_path}. Length: {len(lore_content)} chars.")
            except FileNotFoundError:
                self.app.logger.warning(f"Lore file {generated_lore_path} not found. Proceeding without lore context for backstories.")
                lore_content = "没有可用的整体世界观背景。"
            
            # Load character data
            try:
                all_character_data = read_json(characters_json_path)
                characters = all_character_data.get("characters", [])
                if not characters:
                    self.app.logger.warning(f"No characters found in {characters_json_path}")
                    characters = []
                else:
                    self.app.logger.info(f"Loaded {len(characters)} characters for enhancement from {characters_json_path}")
                    # self.app.logger.debug(f"Raw characters loaded from JSON: {characters}") # ADDED: Log all loaded characters
            except FileNotFoundError:
                self.app.logger.error(f"Character file {characters_json_path} not found. Cannot enhance.", exc_info=True)
                show_error("错误", f"找不到人物文件：{characters_json_path}")
                return
            except (json.JSONDecodeError, ValueError) as e:
                self.app.logger.error(f"Error decoding JSON from {characters_json_path}: {e}. Cannot enhance.", exc_info=True)
                show_error("错误", f"解析人物 JSON 文件失败（{characters_json_path}）：{e}")
                return

            # Identify and sort main characters (Protagonist, Deuteragonist, Antagonist)
            main_roles = ["protagonist", "deuteragonist", "antagonist"]
            main_chars_data = [c for c in characters if c.get("role", "").lower() in main_roles]
            role_priority = {"protagonist": 0, "deuteragonist": 1, "antagonist": 2}
            main_chars_data.sort(key=lambda x: role_priority.get(x.get("role", "").lower(), 99))

            if not main_chars_data:
                self.app.logger.warning("No Protagonist, Deuteragonist, or Antagonist found in characters.json for enhancement.")
                show_warning("警告", "characters.json 中没有找到主角、第二主角或反派。")
                return

            self.app.logger.info(f"Found main characters for enhancement (Count: {len(main_chars_data)}): {[c.get('name', 'NAME N/A') for c in main_chars_data]}")
            # self.app.logger.debug(f"Full main_chars_data content before loop: {main_chars_data}") # ADDED: Log full list before loop

            # --- Loop through main characters to generate backstories ---
            generated_backstories = {} # Store generated backstories

            for i, char_data_item in enumerate(main_chars_data): # Changed char_data to char_data_item and used enumerate
                self.app.logger.info(f"--- Iteration {i} for main character enhancement ---") # ADDED: Iteration log
                self.app.logger.debug(f"Processing char_data_item (type: {type(char_data_item)}): {char_data_item}") # ADDED: Log current item and its type
                
                char_name = char_data_item.get('name', 'Unknown Character') 
                char_role = char_data_item.get('role', 'Unknown Role')
                self.app.logger.info(f"Extracted - Name: '{char_name}', Role: '{char_role}'") # ADDED: Log extracted name/role

                self.app.logger.info(f"--- Generating backstory for: {char_name} ({char_role}) ---") # Original log line

                # Get current genre for appropriate prompt
                params = ui.parameters
                current_genre = params.get("genre", "Sci-Fi")
                current_subgenre = params.get("subgenre", "")
                
                # Build the prompt
                genre_text = f"{current_subgenre} {current_genre}" if current_subgenre else current_genre
                prompt_lines = [
                    f"我正在创作一部{zh_label(current_subgenre)}{zh_label(current_genre)}小说，需要完善关键人物 {char_name}（{zh_label(char_role.capitalize())}）的背景故事。",
                    "请生成详细背景，涵盖其家庭、成长经历、重大人生事件，以及其如何成为故事开始时的自己。",
                    "内容应符合整体世界观和已提供的人物信息，包括年龄、性别和家庭成员。"
                ]

                # Add overall lore
                prompt_lines.append("\n## 整体世界观：")
                prompt_lines.append(lore_content)

                # Add current character details
                prompt_lines.append(f"\n## {char_name}（{zh_label(char_role.capitalize())}）的详细信息：")
                
                # Add basic character information using dict.get()
                # Get character attributes from genre handler
                try:
                    genre_handler = get_genre_handler(current_genre)
                    character_keys = genre_handler.get_character_attributes()
                except ValueError:
                    # Fallback to basic attributes if genre handler not found
                    character_keys = ['age', 'gender', 'title', 'occupation', 'faction', 'faction_role', 
                                    'goals', 'motivations', 'flaws', 'strengths', 'arc']
                
                for key in character_keys:
                    value = char_data_item.get(key)
                    if value:
                        if isinstance(value, list):
                            prompt_lines.append(f"- {zh_field(key)}：{', '.join(value)}")
                        else:
                            prompt_lines.append(f"- {zh_field(key)}：{value}")

                # Add formatted family details using dict.get()
                family_data = char_data_item.get('family', {})
                if family_data: # Check if family_data itself is not empty
                    prompt_lines.append("- 家庭：")
                    parents = family_data.get('parents', [])
                    if parents:
                        parents_str = ", ".join([f"{p.get('name', '无')} ({p.get('relation', '无')}, {p.get('gender', '无')}, {p.get('status', '无')})"
                                               for p in parents])
                        prompt_lines.append(f"  - 父母：{parents_str}")
                    
                    siblings = family_data.get('siblings', [])
                    if siblings:
                        siblings_str = ", ".join([f"{s.get('name', '无')} ({s.get('relation', '无')}, {s.get('gender', '无')})"
                                                for s in siblings])
                        prompt_lines.append(f"  - 兄弟姐妹：{siblings_str}")

                    spouse = family_data.get('spouse') # Can be a dict or None
                    if spouse and isinstance(spouse, dict):
                        prompt_lines.append(f"  - 配偶：{spouse.get('name', '无')}（{spouse.get('gender', '无')}）")

                    children = family_data.get('children', [])
                    if children:
                        children_str = ", ".join([f"{c.get('name', '无')} ({c.get('relation', '无')}, {c.get('gender', '无')})"
                                                for c in children])
                        prompt_lines.append(f"  - 子女：{children_str}")

                # Add previously generated backstories for context
                if generated_backstories:
                    prompt_lines.append("\n## 其他主要人物背景（供衔接参考）：")
                    for name, story in generated_backstories.items():
                        prompt_lines.append(f"### {name} 的背景故事：")
                        prompt_lines.append(story)
                        prompt_lines.append("\n---\n")
                    prompt_lines.append(f"\n请确保为 {char_name} 生成的背景与这些既有背景一致或互补，并形成潜在联系或对照。")

                # Final instruction
                prompt_lines.append("\n现在生成背景故事：")
                prompt = "\n".join(prompt_lines)
                # self.app.logger.debug(f"Backstory prompt for {char_name} (length: {len(prompt)} chars):\n{prompt}") # Old direct logging

                # Save the prompt for this character's backstory to a file
                prompt_base_name = f"background_{char_role.lower().replace(' ', '_').replace('/', '_').replace(':', '_')}_{char_name.lower().replace(' ', '_').replace('/', '_').replace(':', '_')}_prompt"
                prompt_filepath = save_prompt_to_file(output_dir, prompt_base_name, prompt)

                if prompt_filepath:
                    self.app.logger.info(f"Backstory prompt for {char_name} (length {len(prompt)}) saved to: {prompt_filepath}")
                else:
                    self.app.logger.error(f"Failed to save backstory prompt for {char_name} to a file. Prompt length: {len(prompt)}.")
                    if self.app.logger.isEnabledFor(logging.DEBUG):
                        self.app.logger.debug(f"Fallback: Full backstory prompt for {char_name} due to save failure:\n{prompt}")
                    else:
                        self.app.logger.warning(f"Backstory prompt content for {char_name} not logged directly due to length and save failure. Enable DEBUG for full prompt.")

                # Send prompt to LLM
                log_msg_prompt_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
                current_backend = get_backend()
                backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
                self.app.logger.info(f"Sending backstory prompt for {char_name} {log_msg_prompt_source} to LLM (backend: {backend_info})...")
                response = send_prompt(prompt, model=selected_model)

                if not response:
                    self.app.logger.warning(f"Failed to get backstory from LLM for {char_name}. Skipping.")
                    continue
                
                self.app.logger.info(f"Received backstory for {char_name}. Length: {len(response)} chars.")
                # Save the generated backstory
                # Sanitize char_role and char_name for the filename to avoid issues with spaces or special characters
                safe_char_role = char_role.lower().replace(' ', '_').replace('/', '_').replace(':', '_')
                safe_char_name = char_name.lower().replace(' ', '_').replace('/', '_').replace(':', '_')
                base_filename = f"background_{safe_char_role}_{safe_char_name}.md"
                
                # Use structured directory for background files
                lore_dir = self.dir_manager.get_path('lore_dir')
                lore_full_path = os.path.join(output_dir, lore_dir)
                os.makedirs(lore_full_path, exist_ok=True)
                background_filepath = os.path.join(lore_full_path, base_filename)
                write_file(background_filepath, response)
                self.app.logger.info(f"Saved background for {char_name} to {background_filepath}")
                
                # Store for next iteration's context
                generated_backstories[char_name] = response

            self.app.logger.info("--- Main character enhancement process complete! ---")
            # show_success("Success", "Successfully generated backstories for main characters.")

        except Exception as e:
            self.app.logger.error(f"An error occurred during main character enhancement: {e}", exc_info=True)
            # import traceback # No longer needed
            # traceback.print_exc()
            show_error("错误", f"完善主要人物失败：{str(e)}")
