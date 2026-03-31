# GOJEP Tender Extraction Project

This project extracts comprehensive tender listing data from the Government of Jamaica Electronic Procurement (GOJEP) website at [https://www.gojep.gov.jm/](https://www.gojep.gov.jm/).

## Features

- **Automated Web Scraping**: Navigates to GOJEP tender listings automatically
- **AI-Powered Captcha Solving**: Uses Qwen 2.5 VL (Vision Language) model via OpenRouter API
- **🆕 Detail Page Extraction**: Extracts comprehensive data from individual tender detail pages
- **🆕 Database Integration**: Stores data in Supabase PostgreSQL database for persistent storage
- **Multiple Output Formats**: Supports JSON, CSV, Excel, and database storage
- **Robust Error Handling**: Includes retry mechanisms and comprehensive logging
- **Configurable**: Flexible configuration options for different use cases
- **Batch Processing**: Efficiently processes multiple tenders with detail extraction

## Project Structure

```
gojep/                         # repository root (folder name is arbitrary)
├── main.py                    # CLI entry (calls cli.main)
├── pyproject.toml             # package metadata; pip install -e .
├── requirements.txt
├── data/                      # runtime outputs (gitignored except .gitkeep)
│   ├── tenders/               # tender listing + detail JSON extracts
│   ├── awards/                # contract award listing JSON extracts
│   ├── logs/                  # scraper log
│   ├── captcha_images/
│   └── analysis/              # default LLM analysis JSON output
├── cli/                        # extract / analyze subcommands
├── config/                     # settings + Secret Manager helpers
├── modules/                   # data acquisition modules
│   ├── captcha/               # solve_captcha
│   ├── tenders/               # get_tenders, get_tender_details
│   └── awards/                # placeholder scaffolding for future awards
├── analysis/                  # LLM analysis for tenders
├── db/                        # Supabase client + SQL schemas
├── ops/                       # maintenance tasks (reconciliation)
└── tools/                    # DB maintenance scripts
```

## Prerequisites

- Python 3.8 or higher
- Chrome browser installed
- OpenRouter API key (for captcha solving with Qwen 2.5 VL model)

## Installation

1. **Clone or download the project**:
   ```bash
   cd gojep
   ```

2. **Install the package** (recommended):
   ```bash
   pip install -e .
   ```
   Or install dependencies only:
   ```bash
   pip install -r requirements.txt
   ```

3. **Setup environment variables**:
   Create a `.env` file with your API credentials:
   ```env
   # Required: OpenRouter API Key for CAPTCHA solving
   OPENROUTER_API_KEY=your_openrouter_api_key_here
   
   # Optional: Supabase Database (for persistent storage)
   SUPABASE_URL=https://your-project.supabase.co
   SUPABASE_KEY=your_supabase_anon_public_key
   
   # Optional: Settings
   HEADLESS_MODE=True
   LOG_LEVEL=INFO
   ```

4. **🆕 Database Setup (Optional)**:
   If using Supabase for persistent storage:
   - Create a new project at [Supabase](https://supabase.com/)
- Run the SQL schema from `tenders/db/schemas/sql/database_schema.sql` in the SQL editor
   - Copy your project URL and anon key to the `.env` file

## Usage

### Basic Usage

```bash
python main.py extract --help
python main.py analyze --help
```

### Extraction

```bash
python main.py extract --headless --log-level DEBUG
python main.py extract --output-dir data/tenders
python main.py extract --max-pages 5
python main.py extract --max-pages 0
python main.py extract --reconcile-only
```

### Analysis

```bash
python main.py analyze --max-records 10
python main.py analyze --output-file data/analysis/run.json
```

### Maintenance scripts

From the repository root, after `pip install -e .`:

```bash
python -m tools.setup_analysis_table
python -m tools.check_analysis_table
```

## Configuration

The project can be configured through:

1. **Environment variables** (`.env` file)
2. **Command line arguments**
3. **Direct modification of `config/settings.py`**

### Key Configuration Options

- **LLM Settings**: Uses Qwen 2.5 VL model via OpenRouter for vision-based captcha solving
- **Browser Settings**: Headless mode, window size, timeouts
- **Output Settings**: Format, directory, file naming
- **Captcha Settings**: Retry attempts, save path
- **Pagination Settings**: Results per page (default: 100), page limits
- **Logging**: Level, file output

## How It Works

1. **Navigation**: Opens Chrome browser and navigates to GOJEP opportunities page
2. **Captcha Detection**: Identifies captcha element on the page
3. **Image Extraction**: Downloads captcha image
4. **AI Solving**: Sends image to Qwen 2.5 VL model via OpenRouter for solving
5. **Form Submission**: Inputs solution and submits form
6. **Optimization**: Automatically sets results per page to 100 for efficiency
7. **Data Extraction**: Scrapes tender listings from results page
8. **Data Export**: Saves extracted data in chosen format

## Output

The scraper generates timestamped files in the specified output directory:

- `gojep_tenders_YYYYMMDD_HHMMSS.json`
- `gojep_tenders_YYYYMMDD_HHMMSS.csv`
- `gojep_tenders_YYYYMMDD_HHMMSS.xlsx`

### Sample Output Structure (JSON)

```json
[
  {
    "row_number": "1",
    "title": "Supply and Maintenance of Sanitary Bins and Air Fresheners for a period of one (1) year",
    "detail_url": "/epps/cft/prepareViewCfTWS.do?resourceId=7548010",
    "resource_id": "7548010",
    "procuring_entity": "Kingston and St. Andrew Municipal Corporation",
    "description": "Supply and Maintenance of Sanitary Bins and Air Fresheners for a period of one (1) year for the Kingston and St. Andrew Municipal Corporation",
    "submission_deadline": "09/07/2025 10:00:00",
    "submission_deadline_parsed": "2025-07-09T10:00:00",
    "procurement_type": "Services",
    "procedure": "Open - NCB",
    "publication_date": "01/07/2025 14:10:59",
    "publication_date_parsed": "2025-07-01T14:10:59",
    "pdf_url": "https://www.gojep.gov.jm/epps/cft/downloadNoticeForAdvSearch.do?resourceId=7548010",
    "extraction_timestamp": "2024-01-15T10:30:00",
    "source_url": "https://www.gojep.gov.jm/epps/prepareCurrentOpportunities.do"
  }
]
```

## API Key Setup

### OpenRouter API Key

1. Sign up at [https://openrouter.ai/](https://openrouter.ai/)
2. Navigate to the Keys section in your dashboard
3. Create a new API key
4. Add to `.env` file: `OPENROUTER_API_KEY=sk-or-v1-...`

This project uses **Qwen 2.5 VL 32B Instruct**, a state-of-the-art vision-language model that excels at:
- **Image Understanding**: Advanced visual comprehension for complex captchas
- **Text Recognition**: Accurate OCR capabilities for text-based challenges
- **Multimodal Processing**: Combines visual and textual understanding
- **High Performance**: 32B parameter model optimized for accuracy

## Troubleshooting

### Common Issues

1. **Captcha solving fails**:
   - Check OpenRouter API key is valid and has sufficient credits
   - Verify Qwen 2.5 VL model is available on your OpenRouter account
   - Check internet connection

2. **Browser issues**:
   - Update Chrome browser
   - Try running without headless mode
   - Check system permissions

3. **No data extracted**:
   - Website structure may have changed
   - Check logs for specific errors
   - Verify captcha was solved correctly

### Logging

The scraper generates detailed logs:
- Console output for real-time monitoring
- `data/logs/gojep_scraper.log` for detailed debugging

### Debug Mode

Run with debug logging to get detailed information:
```bash
python main.py extract --log-level DEBUG
```

## Extending the Project

### Adding New Data Fields

Modify `extract_tender_details()` in `tenders/scraping/scraper.py` to extract additional fields based on the HTML structure.

### Supporting Additional Websites

Create new scraper classes following the patterns in `tenders/scraping/scraper.py`.

### Custom Output Formats

Extend the `save_data()` method to support additional output formats.

## Legal and Ethical Considerations

- This tool is for legitimate data extraction purposes
- Respect the website's robots.txt and terms of service
- Use reasonable delays between requests
- Don't overload the server with excessive requests

## Contributing

1. Fork the project
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is provided as-is for educational and legitimate business purposes. Please ensure compliance with applicable laws and website terms of service.

## Support

For issues and questions:
1. Check the troubleshooting section above
2. Review the logs for specific error messages
3. Create an issue with detailed error information and system details 